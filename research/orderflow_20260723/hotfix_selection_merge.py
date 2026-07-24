from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "run_research.py"
SOURCE_SHA256 = "4933bdf62afabbbf3d79ef7888bc9eb7eec3f89ed4b447a211260e3851e92cdd"
PATCHED_SHA256 = "623a05f09d564f32667b394131aed345cb4578870a2f710350699825b92f7fcb"

raw = TARGET.read_bytes()
actual = hashlib.sha256(raw).hexdigest()
if actual == PATCHED_SHA256:
    print({"hotfix": "selection_merge_already_applied", "sha256": actual})
    raise SystemExit(0)
if actual != SOURCE_SHA256:
    raise RuntimeError(f"unexpected source engine digest: {actual}")

text = raw.decode("utf-8")
old = '''def select_finalists(dev_refined: pd.DataFrame, selection: pd.DataFrame, stress: pd.DataFrame, specs_by_id: dict[str, Spec]) -> list[Spec]:
    merged = selection.merge(
        stress[["spec_id", "geometric_daily_return", "total_return", "max_drawdown", "profit_factor"]],
        on="spec_id", how="left", suffixes=("_base", "_stress"),
    ).merge(
        dev_refined[["spec_id", "robustness_score", "positive_fold_share", "worst_fold_g", "top_5_positive_share"]],
        on="spec_id", how="left",
    )
    merged["selection_score"] = (
        merged.geometric_daily_return_base
        + 0.75 * merged.geometric_daily_return_stress.fillna(-1.0)
        + 0.50 * merged.worst_fold_g
        - 0.10 * merged.max_drawdown_base.abs() / 365.0
        - 0.0002 * (merged.top_5_positive_share - 0.50).clip(lower=0.0)
    )
    eligible = merged.loc[
        (merged.total_return_base > 0)
        & (merged.total_return_stress > 0)
        & (merged.trades >= 25)
        & (merged.positive_fold_share >= 0.5)
        & (merged.top_5_positive_share <= 0.75)
    ].sort_values("selection_score", ascending=False)
'''
new = '''def select_finalists(dev_refined: pd.DataFrame, selection: pd.DataFrame, stress: pd.DataFrame, specs_by_id: dict[str, Spec]) -> list[Spec]:
    # Selection rows also contain fold diagnostics. Namespace the development
    # gate explicitly so pandas cannot create ambiguous *_x/*_y columns.
    dev_gate = dev_refined[[
        "spec_id", "robustness_score", "positive_fold_share", "worst_fold_g", "top_5_positive_share",
    ]].rename(columns={
        "robustness_score": "dev_robustness_score",
        "positive_fold_share": "dev_positive_fold_share",
        "worst_fold_g": "dev_worst_fold_g",
        "top_5_positive_share": "dev_top_5_positive_share",
    })
    merged = selection.merge(
        stress[["spec_id", "geometric_daily_return", "total_return", "max_drawdown", "profit_factor"]],
        on="spec_id", how="left", suffixes=("_base", "_stress"),
    ).merge(dev_gate, on="spec_id", how="left")
    merged["selection_score"] = (
        merged.geometric_daily_return_base
        + 0.75 * merged.geometric_daily_return_stress.fillna(-1.0)
        + 0.50 * merged.dev_worst_fold_g
        - 0.10 * merged.max_drawdown_base.abs() / 365.0
        - 0.0002 * (merged.dev_top_5_positive_share - 0.50).clip(lower=0.0)
    )
    eligible = merged.loc[
        (merged.total_return_base > 0)
        & (merged.total_return_stress > 0)
        & (merged.trades >= 25)
        & (merged.dev_positive_fold_share >= 0.5)
        & (merged.dev_top_5_positive_share <= 0.75)
    ].sort_values("selection_score", ascending=False)
'''
if text.count(old) != 1:
    raise RuntimeError("selection hotfix target not found exactly once")
TARGET.write_text(text.replace(old, new), encoding="utf-8")
patched = hashlib.sha256(TARGET.read_bytes()).hexdigest()
if patched != PATCHED_SHA256:
    raise RuntimeError(f"patched engine digest mismatch: expected {PATCHED_SHA256}, got {patched}")
print({"hotfix": "selection_merge_applied", "sha256": patched})
