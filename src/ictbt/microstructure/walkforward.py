from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


DEFAULT_L2 = 1.0
DEFAULT_MIN_TRAIN_SAMPLES = 100
DEFAULT_EMBARGO = pd.Timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class L2LogisticModel:
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    intercept: float
    coefficients: tuple[float, ...]
    l2: float
    iterations: int
    converged: bool

    def predict_proba(self, features: pd.DataFrame) -> pd.Series:
        missing = [name for name in self.feature_names if name not in features]
        if missing:
            raise ValueError(f"prediction features are missing columns: {missing}")
        matrix = features.loc[:, self.feature_names].to_numpy(dtype=float, copy=True)
        if not np.isfinite(matrix).all():
            raise ValueError("prediction features must be finite")
        means = np.asarray(self.means, dtype=float)
        scales = np.asarray(self.scales, dtype=float)
        standardized = (matrix - means) / scales
        logits = self.intercept + standardized @ np.asarray(
            self.coefficients, dtype=float
        )
        probabilities = _sigmoid(logits)
        return pd.Series(probabilities, index=features.index, name="probability")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_cutoff: pd.Timestamp
    train_samples: int
    test_samples: int
    probability_threshold: float
    model: L2LogisticModel


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    predictions: pd.DataFrame
    folds: tuple[WalkForwardFold, ...]


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be valid")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def _sigmoid(logits: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(logits, dtype=float), -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _validate_training(
    features: pd.DataFrame,
    labels: pd.Series,
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    if features.empty:
        raise ValueError("training features cannot be empty")
    names = tuple(str(column) for column in features.columns)
    if len(set(names)) != len(names):
        raise ValueError("training feature names must be unique")
    matrix = features.to_numpy(dtype=float, copy=True)
    target = pd.Series(labels, index=features.index).to_numpy(dtype=float, copy=True)
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise ValueError("training requires at least one feature")
    if len(target) != len(matrix):
        raise ValueError("training labels and features differ in length")
    if not np.isfinite(matrix).all() or not np.isfinite(target).all():
        raise ValueError("training values must be finite")
    unique = set(np.unique(target))
    if not unique.issubset({0.0, 1.0}) or len(unique) != 2:
        raise ValueError("training labels must contain both binary classes")
    return matrix, target, names


def fit_l2_logistic(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    l2: float = DEFAULT_L2,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
) -> L2LogisticModel:
    """Fit deterministic standardized L2 logistic regression by Newton steps."""

    penalty = float(l2)
    if not math.isfinite(penalty) or penalty <= 0:
        raise ValueError("l2 must be finite and positive")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance must be finite and positive")

    matrix, target, names = _validate_training(features, labels)
    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0, ddof=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    standardized = (matrix - means) / scales
    design = np.column_stack((np.ones(len(standardized)), standardized))
    beta = np.zeros(design.shape[1], dtype=float)
    regularizer = np.eye(design.shape[1], dtype=float) * penalty
    regularizer[0, 0] = 0.0
    converged = False
    iterations = 0

    for iterations in range(1, max_iterations + 1):
        probabilities = _sigmoid(design @ beta)
        gradient = design.T @ (probabilities - target) + regularizer @ beta
        weights = np.clip(probabilities * (1.0 - probabilities), 1e-9, None)
        hessian = design.T @ (design * weights[:, None]) + regularizer
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian) @ gradient
        beta_next = beta - step
        if not np.isfinite(beta_next).all():
            raise ValueError("logistic fit produced non-finite coefficients")
        beta = beta_next
        if float(np.max(np.abs(step))) <= tolerance:
            converged = True
            break

    return L2LogisticModel(
        feature_names=names,
        means=tuple(float(value) for value in means),
        scales=tuple(float(value) for value in scales),
        intercept=float(beta[0]),
        coefficients=tuple(float(value) for value in beta[1:]),
        l2=penalty,
        iterations=iterations,
        converged=converged,
    )


def economic_probability_threshold(net_r: pd.Series) -> float:
    """Return the train-only break-even win probability for realized payoff."""

    values = pd.to_numeric(net_r, errors="raise").astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("net R values must be finite")
    wins = values.loc[values > 0]
    losses = values.loc[values < 0]
    if wins.empty or losses.empty:
        raise ValueError("break-even probability requires wins and losses")
    average_win = float(wins.mean())
    average_loss = float(losses.mean())
    threshold = -average_loss / (average_win - average_loss)
    if not 0 < threshold < 1:
        raise ValueError("break-even probability must lie inside (0, 1)")
    return threshold


def _validate_panel(
    features: pd.DataFrame,
    net_r: pd.Series,
    known_at: pd.Series,
    label_end: pd.Series,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    if not features.index.is_unique:
        raise ValueError("scene index must be unique")
    aligned_r = pd.Series(net_r).reindex(features.index)
    aligned_known = pd.Series(known_at).reindex(features.index)
    aligned_end = pd.Series(label_end).reindex(features.index)
    if aligned_r.isna().any() or aligned_known.isna().any() or aligned_end.isna().any():
        raise ValueError("features, net R and clocks must share the same complete index")
    aligned_r = pd.to_numeric(aligned_r, errors="raise").astype(float)
    aligned_known = pd.Series(
        pd.to_datetime(aligned_known, utc=True, errors="raise"),
        index=features.index,
    )
    aligned_end = pd.Series(
        pd.to_datetime(aligned_end, utc=True, errors="raise"),
        index=features.index,
    )
    if bool((aligned_end < aligned_known).any()):
        raise ValueError("label_end cannot precede scene known_at")
    matrix = features.astype(float)
    if not np.isfinite(matrix.to_numpy()).all() or not np.isfinite(aligned_r.to_numpy()).all():
        raise ValueError("walk-forward panel values must be finite")
    return matrix, aligned_r, aligned_known, aligned_end


def _month_starts(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, ...]:
    first = start.tz_localize(None).to_period("M")
    last = (end - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return tuple(
        pd.Timestamp(period.start_time, tz="UTC")
        for period in pd.period_range(first, last, freq="M")
    )


def purged_expanding_monthly_predict(
    features: pd.DataFrame,
    net_r: pd.Series,
    known_at: pd.Series,
    label_end: pd.Series,
    *,
    test_start: object,
    test_end: object,
    embargo: pd.Timedelta = DEFAULT_EMBARGO,
    minimum_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
    l2: float = DEFAULT_L2,
) -> WalkForwardResult:
    """Generate out-of-sample monthly predictions with train-only economics.

    A sample is trainable only when its outcome was completely known before the
    test month minus the embargo. Every test row is predicted by a model fit
    before that row's month began.
    """

    start = _utc(test_start, name="test_start")
    end = _utc(test_end, name="test_end")
    if end <= start:
        raise ValueError("test_end must follow test_start")
    gap = pd.Timedelta(embargo)
    if pd.isna(gap) or gap < pd.Timedelta(0):
        raise ValueError("embargo must be non-negative")
    if minimum_train_samples <= 0:
        raise ValueError("minimum_train_samples must be positive")

    matrix, returns, known, outcomes = _validate_panel(
        features, net_r, known_at, label_end
    )
    rows: list[pd.DataFrame] = []
    folds: list[WalkForwardFold] = []

    for month_start in _month_starts(start, end):
        month_end = min(month_start + pd.offsets.MonthBegin(1), end)
        effective_start = max(month_start, start)
        test_mask = (known >= effective_start) & (known < month_end)
        if not bool(test_mask.any()):
            continue
        train_cutoff = effective_start - gap
        train_mask = (known < effective_start) & (outcomes < train_cutoff)
        train_count = int(train_mask.sum())
        if train_count < minimum_train_samples:
            raise ValueError(
                f"insufficient train samples before {effective_start.isoformat()}: "
                f"{train_count} < {minimum_train_samples}"
            )
        train_features = matrix.loc[train_mask]
        train_r = returns.loc[train_mask]
        labels = (train_r > 0).astype(float)
        model = fit_l2_logistic(train_features, labels, l2=l2)
        threshold = economic_probability_threshold(train_r)
        probabilities = model.predict_proba(matrix.loc[test_mask])
        fold_rows = pd.DataFrame(
            {
                "known_at": known.loc[test_mask],
                "label_end": outcomes.loc[test_mask],
                "net_r": returns.loc[test_mask],
                "probability": probabilities,
                "probability_threshold": threshold,
                "accepted": probabilities >= threshold,
                "train_samples": train_count,
                "train_cutoff": train_cutoff,
                "test_month": effective_start,
            },
            index=matrix.index[test_mask],
        )
        rows.append(fold_rows)
        folds.append(
            WalkForwardFold(
                test_start=effective_start,
                test_end=month_end,
                train_cutoff=train_cutoff,
                train_samples=train_count,
                test_samples=int(test_mask.sum()),
                probability_threshold=threshold,
                model=model,
            )
        )

    predictions = (
        pd.concat(rows).sort_values("known_at", kind="mergesort")
        if rows
        else pd.DataFrame(
            columns=(
                "known_at",
                "label_end",
                "net_r",
                "probability",
                "probability_threshold",
                "accepted",
                "train_samples",
                "train_cutoff",
                "test_month",
            )
        )
    )
    return WalkForwardResult(predictions=predictions, folds=tuple(folds))


__all__ = [
    "DEFAULT_EMBARGO",
    "DEFAULT_L2",
    "DEFAULT_MIN_TRAIN_SAMPLES",
    "L2LogisticModel",
    "WalkForwardFold",
    "WalkForwardResult",
    "economic_probability_threshold",
    "fit_l2_logistic",
    "purged_expanding_monthly_predict",
]
