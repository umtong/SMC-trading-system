from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_btc_state_feature_availability.py SCRIPT")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

replacements = [
(
'''def finite_training_mask(X: pd.DataFrame, y: np.ndarray, index: pd.DatetimeIndex, start: str, end: str, horizon: int) -> np.ndarray:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    # Purge labels whose exit would cross the training boundary.
    purge = pd.Timedelta(minutes=5 * (LATENCY_BARS + 1 + horizon))
    return (
        (index >= start_ts) & (index < end_ts - purge) & np.isfinite(y) &
        X.notna().sum(axis=1).to_numpy() >= int(0.70 * X.shape[1])
    )


def eval_mask(X: pd.DataFrame, index: pd.DatetimeIndex, start: str, end: str, horizon: int) -> np.ndarray:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    purge = pd.Timedelta(minutes=5 * (LATENCY_BARS + 1 + horizon))
    return (
        (index >= start_ts) & (index < end_ts - purge) &
        X.notna().sum(axis=1).to_numpy() >= int(0.70 * X.shape[1])
    )
''',
'''def minimum_observed_features(X: pd.DataFrame) -> int:
    # The public feature history is heterogeneous: L2 depth starts later than
    # price/flow/OI. Do not erase valid early rows merely because optional
    # channels did not yet exist. Models impute missing optional state and add
    # missingness indicators; rows still need a fixed minimum causal core.
    return max(8, min(20, int(math.ceil(0.20 * max(X.shape[1], 1)))))


def finite_training_mask(X: pd.DataFrame, y: np.ndarray, index: pd.DatetimeIndex, start: str, end: str, horizon: int) -> np.ndarray:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    # Purge labels whose exit would cross the training boundary.
    purge = pd.Timedelta(minutes=5 * (LATENCY_BARS + 1 + horizon))
    return (
        (index >= start_ts) & (index < end_ts - purge) & np.isfinite(y) &
        (X.notna().sum(axis=1).to_numpy() >= minimum_observed_features(X))
    )


def eval_mask(X: pd.DataFrame, index: pd.DatetimeIndex, start: str, end: str, horizon: int) -> np.ndarray:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    purge = pd.Timedelta(minutes=5 * (LATENCY_BARS + 1 + horizon))
    return (
        (index >= start_ts) & (index < end_ts - purge) &
        (X.notna().sum(axis=1).to_numpy() >= minimum_observed_features(X))
    )
'''
),
(
'''    def col(name: str) -> np.ndarray:
        return pd.to_numeric(X[name], errors="coerce").fillna(0.0).to_numpy(float)
''',
'''    def col(name: str) -> np.ndarray:
        if name not in X.columns:
            return np.zeros(len(X), dtype=float)
        return pd.to_numeric(X[name], errors="coerce").fillna(0.0).to_numpy(float)
'''
),
(
'''    cfg = STAGES[stage]
    rows: list[dict[str, Any]] = []
    model_metadata: dict[str, dict[str, Any]] = {}
''',
'''    cfg = STAGES[stage]
    # Select columns solely from the stage training interval. A channel is kept
    # if it has at least one historical observation before the training cut; no
    # evaluation-period availability can influence the feature contract.
    train_time = (X.index >= pd.Timestamp(cfg["train_start"])) & (X.index < pd.Timestamp(cfg["train_end"]))
    train_availability = X.loc[train_time].notna().mean()
    available_columns = sorted(train_availability.loc[train_availability > 0.0].index.tolist())
    if not available_columns:
        raise RuntimeError("no causally available feature columns in training interval")
    X_stage = X.loc[:, available_columns]
    rows: list[dict[str, Any]] = []
    model_metadata: dict[str, dict[str, Any]] = {}
'''
),
(
'''        y = labels[model_spec.horizon]
        train_mask = finite_training_mask(X, y, X.index, cfg["train_start"], cfg["train_end"], model_spec.horizon)
        ev_mask = eval_mask(X, X.index, cfg["eval_start"], cfg["eval_end"], model_spec.horizon)
        train_scores, scores, metadata = fit_and_score(model_spec, X, y, train_mask)
        model_metadata[model_spec.model_id] = metadata
''',
'''        y = labels[model_spec.horizon]
        train_mask = finite_training_mask(X_stage, y, X_stage.index, cfg["train_start"], cfg["train_end"], model_spec.horizon)
        ev_mask = eval_mask(X_stage, X_stage.index, cfg["eval_start"], cfg["eval_end"], model_spec.horizon)
        if int(train_mask.sum()) == 0:
            raise RuntimeError(f"empty causal training sample for {model_spec.model_id}")
        if int(ev_mask.sum()) == 0:
            raise RuntimeError(f"empty causal evaluation sample for {model_spec.model_id}")
        train_scores, scores, metadata = fit_and_score(model_spec, X_stage, y, train_mask)
        metadata["feature_columns"] = available_columns
        metadata["feature_column_count"] = len(available_columns)
        metadata["minimum_observed_features"] = minimum_observed_features(X_stage)
        metadata["minimum_training_availability"] = float(train_availability.loc[available_columns].min())
        metadata["train_rows_after_availability_gate"] = int(train_mask.sum())
        metadata["eval_rows_after_availability_gate"] = int(ev_mask.sum())
        model_metadata[model_spec.model_id] = metadata
'''
),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one feature-availability patch target; found {count}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
