from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

ARMS = (
    "A_LEADER_V03_BREAK_RETEST_PLUS_V05",
    "B_V07_FIRST_RETURN_LIMIT",
    "C_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
    "D_LEADER_PLUS_V07_FIRST_RETURN_LIMIT",
    "E_LEADER_PLUS_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute committed portfolio metrics and test economic hypotheses."
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("results/easychart_v07_scene_families/trade_ledger.csv"),
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("results/easychart_v07_scene_families/summary.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/verification_growth_hypotheses"),
    )
    return parser.parse_args()


def _finite(value: Any, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _log_growth(rows: pd.DataFrame) -> float:
    terms: list[float] = []
    for before, after in zip(rows["equity_before"], rows["equity_after"]):
        left = _finite(before, name="equity_before")
        right = _finite(after, name="equity_after")
        if left <= 0 or right <= 0:
            raise ValueError("log growth requires positive equity")
        terms.append(math.log(right / left))
    return math.fsum(terms)


def _max_drawdown(rows: pd.DataFrame, initial_equity: float = 10_000.0) -> float:
    peak = float(initial_equity)
    worst = 0.0
    ordered = rows.sort_values(["closed_at", "entry_time", "authority_id"])
    for equity in ordered["equity_after"]:
        value = _finite(equity, name="equity_after")
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def _profit_factor(rows: pd.DataFrame) -> float | None:
    positive = float(rows.loc[rows["net_pnl"] > 0, "net_pnl"].sum())
    negative = -float(rows.loc[rows["net_pnl"] < 0, "net_pnl"].sum())
    return None if negative == 0 else positive / negative


def _metrics(rows: pd.DataFrame) -> dict[str, Any]:
    net_r = [float(value) for value in rows["net_r"]]
    wins = [value for value in net_r if value > 0]
    losses = [value for value in net_r if value < 0]
    log_growth = _log_growth(rows)
    return {
        "trades": int(len(rows)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": None if not net_r else len(wins) / len(net_r),
        "net_r": math.fsum(net_r),
        "average_net_r": None if not net_r else statistics.fmean(net_r),
        "average_win_r": None if not wins else statistics.fmean(wins),
        "average_loss_r": None if not losses else statistics.fmean(losses),
        "profit_factor": _profit_factor(rows),
        "median_target_r": None
        if rows.empty
        else statistics.median(float(value) for value in rows["target_r"]),
        "partial_enabled": int((rows["target_r"] >= 1.4 - 1e-12).sum()),
        "net_log_growth": log_growth,
        "cumulative_return": math.expm1(log_growth),
        "max_drawdown_fraction": _max_drawdown(rows),
        "max_notional_to_equity": None
        if rows.empty
        else float(rows["notional_to_equity"].max()),
    }


def _assert_close(
    actual: Any,
    expected: Any,
    *,
    name: str,
    tolerance: float = 1e-9,
) -> None:
    if expected is None:
        if actual is not None:
            raise AssertionError(f"{name}: expected None, got {actual!r}")
        return
    left = float(actual)
    right = float(expected)
    if not math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{name}: expected {right!r}, got {left!r}")


def _verify_committed_summary(
    global_rows: pd.DataFrame,
    reference: dict[str, Any],
) -> dict[str, Any]:
    checked: dict[str, Any] = {}
    for arm in ARMS:
        rows = global_rows.loc[global_rows["arm"] == arm].copy()
        actual = _metrics(rows)
        expected = reference["arms"][arm]
        if actual["trades"] != int(expected["trades"]):
            raise AssertionError(
                f"{arm}.trades: expected {expected['trades']}, got {actual['trades']}"
            )
        for field in (
            "win_rate",
            "net_r",
            "average_net_r",
            "profit_factor",
            "median_target_r",
            "net_log_growth",
            "cumulative_return",
            "max_drawdown_fraction",
        ):
            _assert_close(
                actual[field],
                expected[field],
                name=f"{arm}.{field}",
                tolerance=1e-8,
            )
        checked[arm] = actual
    return checked


def _member_flags(source_id: Any) -> dict[str, bool]:
    text = "" if pd.isna(source_id) else str(source_id).lower()
    return {
        "contains_pivot": ":pivot:" in text,
        "contains_order_block": ":ob:" in text,
        "contains_fvg": ":fvg:" in text,
    }


def _fixed_trade_set_diagnostic(rows: pd.DataFrame) -> dict[str, Any]:
    """Evaluate already-observed trades without re-routing the global slot."""

    result = _metrics(rows)
    result["method"] = "fixed_trade_set_log_factor_diagnostic_not_full_replay"
    return result


def _target_diagnostics(v07_next_open: pd.DataFrame) -> dict[str, Any]:
    rows = v07_next_open.copy()
    flags = rows["target_source_id"].map(_member_flags).apply(pd.Series)
    rows = pd.concat([rows.reset_index(drop=True), flags.reset_index(drop=True)], axis=1)

    by_kind: dict[str, Any] = {}
    for kind, group in rows.groupby("target_kind", dropna=False):
        by_kind[str(kind)] = _fixed_trade_set_diagnostic(group)

    membership: dict[str, Any] = {}
    for column in ("contains_pivot", "contains_order_block", "contains_fvg"):
        membership[column] = {
            "yes": _fixed_trade_set_diagnostic(rows.loc[rows[column]]),
            "no": _fixed_trade_set_diagnostic(rows.loc[~rows[column]]),
        }

    minimum_target_r_for_65pct_break_even = (1.0 / 0.65) - 1.0
    diagnostics = {
        "all": _fixed_trade_set_diagnostic(rows),
        "by_reported_target_kind": by_kind,
        "by_preserved_source_membership": membership,
        "counterfactual_fixed_trade_sets": {
            "exclude_reported_fvg": _fixed_trade_set_diagnostic(
                rows.loc[rows["target_kind"] != "fvg"]
            ),
            "require_pivot_member": _fixed_trade_set_diagnostic(
                rows.loc[rows["contains_pivot"]]
            ),
            "target_r_supports_65pct_or_lower_break_even": _fixed_trade_set_diagnostic(
                rows.loc[rows["target_r"] >= minimum_target_r_for_65pct_break_even]
            ),
            "pivot_member_and_65pct_gate": _fixed_trade_set_diagnostic(
                rows.loc[
                    rows["contains_pivot"]
                    & (rows["target_r"] >= minimum_target_r_for_65pct_break_even)
                ]
            ),
        },
        "minimum_target_r_for_65pct_break_even": minimum_target_r_for_65pct_break_even,
    }

    pivot = by_kind.get("pivot")
    fvg = by_kind.get("fvg")
    all_metrics = diagnostics["all"]
    if pivot is None or fvg is None:
        raise AssertionError("V0.7 next-open must contain both pivot and FVG target groups")
    if float(pivot["net_r"]) <= 0:
        raise AssertionError("committed pivot target group is not positive")
    if float(fvg["net_r"]) >= 0:
        raise AssertionError("committed FVG target group is not negative")
    if float(all_metrics["average_win_r"]) >= 0.25:
        raise AssertionError("V0.7 next-open average win is not as small as diagnosed")
    if float(all_metrics["median_target_r"]) >= 0.30:
        raise AssertionError("V0.7 next-open median target R is not below 0.30R")
    if int(all_metrics["partial_enabled"]) != 0:
        raise AssertionError("V0.7 next-open unexpectedly contains a 1.4R trade")
    return diagnostics


def _risk_diagnostics(global_rows: pd.DataFrame) -> dict[str, Any]:
    ratios = global_rows["notional_to_equity"].astype(float)
    result: dict[str, Any] = {
        "trades": int(len(global_rows)),
        "max_notional_to_equity": float(ratios.max()),
        "trades_above_3x": int((ratios > 3.0 + 1e-12).sum()),
        "trades_above_5x": int((ratios > 5.0 + 1e-12).sum()),
        "trades_above_10x": int((ratios > 10.0 + 1e-12).sum()),
        "by_arm": {},
    }
    for arm, group in global_rows.groupby("arm"):
        arm_ratios = group["notional_to_equity"].astype(float)
        result["by_arm"][str(arm)] = {
            "trades": int(len(group)),
            "max_notional_to_equity": float(arm_ratios.max()),
            "trades_above_3x": int((arm_ratios > 3.0 + 1e-12).sum()),
            "trades_above_5x": int((arm_ratios > 5.0 + 1e-12).sum()),
            "trades_above_10x": int((arm_ratios > 10.0 + 1e-12).sum()),
        }
    if result["trades_above_5x"] <= 0:
        raise AssertionError("expected at least one committed trade above 5x notional/equity")
    return result


def _markdown(report: dict[str, Any]) -> str:
    checked = report["committed_summary_recomputation"]
    target = report["v07_next_open_target_diagnostics"]
    risk = report["risk_diagnostics"]
    lines = [
        "# Growth hypothesis verification",
        "",
        f"Commit ledger: `{report['ledger']}`",
        "",
        "## Recomputed global arms",
        "",
        "| arm | trades | net R | avg R | win rate | PF | MDD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        item = checked[arm]
        lines.append(
            f"| {arm} | {item['trades']} | {item['net_r']:+.6f} | "
            f"{item['average_net_r']:+.6f} | {item['win_rate']:.2%} | "
            f"{item['profit_factor']:.6f} | {item['max_drawdown_fraction']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## V0.7 next-open target diagnosis",
            "",
            "| group | trades | net R | avg R |",
            "|---|---:|---:|---:|",
        ]
    )
    for kind, item in target["by_reported_target_kind"].items():
        lines.append(
            f"| {kind} | {item['trades']} | {item['net_r']:+.6f} | "
            f"{item['average_net_r']:+.6f} |"
        )
    lines.extend(
        [
            "",
            "The counterfactual rows are fixed-trade-set diagnostics, not a rerouted replay.",
            "",
            "## Exposure audit",
            "",
            f"- Maximum notional/equity: **{risk['max_notional_to_equity']:.3f}x**",
            f"- Trades above 3x: **{risk['trades_above_3x']}**",
            f"- Trades above 5x: **{risk['trades_above_5x']}**",
            f"- Trades above 10x: **{risk['trades_above_10x']}**",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = _args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ledger = pd.read_csv(args.ledger)
    reference = json.loads(args.summary.read_text(encoding="utf-8-sig"))
    global_rows = ledger.loc[ledger["scope"] == "global_portfolio"].copy()
    if global_rows.empty:
        raise AssertionError("committed ledger contains no global_portfolio rows")

    checked = _verify_committed_summary(global_rows, reference)
    v07_next_open = global_rows.loc[
        global_rows["arm"] == "C_V07_BOUNDARY_ACCEPT_NEXT_OPEN"
    ].copy()
    target_diagnostics = _target_diagnostics(v07_next_open)
    risk_diagnostics = _risk_diagnostics(global_rows)

    report = {
        "status": "PASS",
        "ledger": str(args.ledger),
        "summary": str(args.summary),
        "committed_summary_recomputation": checked,
        "v07_next_open_target_diagnostics": target_diagnostics,
        "risk_diagnostics": risk_diagnostics,
        "interpretation_limits": [
            "The committed-ledger recomputation proves arithmetic and diagnostic claims, not unseen-period profitability.",
            "Filtered fixed-trade-set diagnostics do not reroute the global slot and are not strategy backtests.",
            "A new scene family still requires chronological replay on raw OHLCV and out-of-sample validation.",
        ],
    }
    (args.output_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output_dir / "verification_report.md").write_text(
        _markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
