from __future__ import annotations
from pathlib import Path
import sys

NEW_MONTHLY = r'''def _bayes_stats(train: pd.DataFrame, current: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    out = current.copy()
    base = train.copy()
    base["context_key"] = (
        base["group_key"].astype(str) + "|s" + base["session"].astype(str)
        + "|t" + base["regime_trend"].astype(str) + "|v" + base["regime_vol"].astype(str)
    )
    out["context_key"] = (
        out["group_key"].astype(str) + "|s" + out["session"].astype(str)
        + "|t" + out["regime_trend"].astype(str) + "|v" + out["regime_vol"].astype(str)
    )

    def stats(source: pd.DataFrame, key: str, prior_n: float) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        g = source.groupby(key, sort=False).agg(
            n=("net_r", "size"), sum_r=("net_r", "sum"),
            sum_sq=("net_r", lambda x: float(np.square(x).sum())),
            cat=("cat_loss", "sum"), filled=("filled", "sum"),
        )
        mean = g["sum_r"] / (g["n"] + prior_n)
        raw_mean = g["sum_r"] / g["n"].clip(lower=1)
        variance = (g["sum_sq"] / g["n"].clip(lower=1) - raw_mean.square()).clip(lower=0.0)
        uncertainty = np.sqrt(variance / (g["n"] + prior_n))
        cat = (g["cat"] + 2.0) / (g["n"] + 12.0)
        fill = (g["filled"] + 5.0) / (g["n"] + 10.0)
        return mean, uncertainty, cat, fill

    long_mean, long_u, long_cat, long_fill = stats(base, "group_key", 120.0)
    recent_source = base.loc[base["known_time"] >= cutoff - pd.Timedelta(days=240)]
    recent_mean, recent_u, recent_cat, recent_fill = stats(recent_source, "group_key", 70.0)
    context_mean, context_u, context_cat, context_fill = stats(base, "context_key", 90.0)

    out["edge_long"] = out["group_key"].map(long_mean).fillna(0.0)
    out["edge_recent"] = out["group_key"].map(recent_mean).fillna(0.0)
    out["edge_context"] = out["context_key"].map(context_mean).fillna(0.0)
    out["uncertainty"] = (
        0.45 * out["group_key"].map(long_u).fillna(0.35)
        + 0.35 * out["group_key"].map(recent_u).fillna(0.35)
        + 0.20 * out["context_key"].map(context_u).fillna(0.35)
    )
    out["pred_cat"] = (
        0.35 * out["group_key"].map(long_cat).fillna(0.20)
        + 0.45 * out["group_key"].map(recent_cat).fillna(0.20)
        + 0.20 * out["context_key"].map(context_cat).fillna(0.20)
    )
    out["pred_fill"] = (
        0.30 * out["group_key"].map(long_fill).fillna(0.50)
        + 0.50 * out["group_key"].map(recent_fill).fillna(0.50)
        + 0.20 * out["context_key"].map(context_fill).fillna(0.50)
    )
    out["pred_r"] = 0.35 * out["edge_long"] + 0.45 * out["edge_recent"] + 0.20 * out["edge_context"]
    strength = np.tanh(out["event_strength"].fillna(0.0).to_numpy(float) / 3.0)
    out["model_score"] = (
        out["pred_r"] - 0.70 * out["pred_cat"] - 0.55 * out["uncertainty"]
        + 0.015 * strength + 0.02 * (out["pred_fill"] - 0.5)
    )
    out["group_edge_recent"] = out["edge_recent"]
    return out


def monthly_predictions(
    candidates: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    mode: str,
    lookback_months: int,
    order_rates: tuple[float, ...],
) -> pd.DataFrame:
    outputs: list[pd.DataFrame] = []
    months = pd.date_range(start, end, freq="MS", tz="UTC", inclusive="left")
    for month_idx, cutoff in enumerate(months):
        next_month = cutoff + pd.offsets.MonthBegin(1)
        lookback_start = cutoff - pd.DateOffset(months=lookback_months)
        cal_start = cutoff - pd.DateOffset(months=3)
        base_train = candidates.loc[
            (candidates["known_time"] < cal_start - PURGE)
            & (candidates["signal_time"] >= lookback_start)
        ].copy()
        calibration = candidates.loc[
            (candidates["signal_time"] >= cal_start)
            & (candidates["known_time"] < cutoff - PURGE)
        ].copy()
        train_full = candidates.loc[
            (candidates["known_time"] < cutoff - PURGE)
            & (candidates["signal_time"] >= lookback_start)
        ].copy()
        current = candidates.loc[
            (candidates["signal_time"] >= cutoff)
            & (candidates["signal_time"] < next_month)
        ].copy()
        if len(base_train) < 20_000 or len(calibration) < 4_000 or current.empty:
            continue
        calibration = _bayes_stats(base_train, calibration, cal_start)
        cal_days = max(30.0, (cutoff - cal_start).total_seconds() / 86400.0)
        cutoffs: dict[float, float] = {}
        for rate in order_rates:
            wanted = max(20, int(rate * cal_days))
            q = max(0.0, 1.0 - wanted / max(len(calibration), 1))
            cutoffs[rate] = float(np.quantile(calibration["model_score"].to_numpy(float), q))
        current = _bayes_stats(train_full, current, cutoff)
        current["model_mode"] = "bayes"
        current["lookback_months"] = lookback_months
        current["procedure"] = f"bayes_{lookback_months}m"
        for rate, threshold in cutoffs.items():
            current[f"cutoff_{rate:g}"] = threshold
        outputs.append(current)
        print(f"BAYES MONTH {cutoff.date()} lookback={lookback_months} train={len(train_full):,} cal={len(calibration):,} current={len(current):,}", flush=True)
    return pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame()


'''

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_causal_wf_v5_bayes.py SCRIPT")
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    start = text.index("def monthly_predictions(")
    end = text.index("def simulate_policy(", start)
    text = text[:start] + NEW_MONTHLY + text[end:]
    old = 'for mode in ("mean",):\n        for lookback in (30,):'
    new = 'for mode in ("bayes",):\n        for lookback in (18, 30, 42):'
    if old not in text:
        raise RuntimeError("main procedure anchor missing")
    text = text.replace(old, new, 1)
    text = text.replace("random_policies(procedures, order_rates, 260)", "random_policies(procedures, order_rates, 500)", 1)
    path.write_text(text, encoding="utf-8")

if __name__ == "__main__":
    main()
