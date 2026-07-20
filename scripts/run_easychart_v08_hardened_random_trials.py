from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path

import run_easychart_v08_random_trials as base

from ictbt.easychart_v0.domain import B1Subtype
from ictbt.easychart_v0.research_governance import (
    evaluate_promotion_eligibility,
    growth_feasibility,
    summarize_trial_overlap,
)
from ictbt.easychart_v0.research_protocol import EvaluationWindow, TrialSpec
from ictbt.easychart_v0.v04 import build_baseline_event_authorities
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result
from ictbt.easychart_v0.v08_hardened import (
    build_v08_hardened_scene_family_result,
)
from ictbt.easychart_v0.v08_intraday_hardened import (
    build_v08_intraday_hardened_result,
)


ARMS = (
    "leader",
    "leader_plus_v08_hardened_htf",
    "leader_plus_v08_hardened_intraday",
    "leader_plus_all_v08_hardened",
)
_LAST_ARGS: argparse.Namespace | None = None


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hardened 2022-2026 random-window EasyChart/SMC portfolio research"
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/binance_um_5m"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v08_hardened_random_trials"),
    )
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    parser.add_argument("--score-days-per-year", type=int, default=28)
    parser.add_argument("--warmup-days", type=int, default=35)
    parser.add_argument("--exit-extension-days", type=int, default=7)
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--v08-minimum-target-r", type=float, default=0.75)
    parser.add_argument("--v08-displacement-multiple", type=float, default=1.20)
    parser.add_argument("--intraday-minimum-target-r", type=float, default=0.65)
    parser.add_argument("--intraday-displacement-multiple", type=float, default=1.10)
    parser.add_argument("--intraday-maximum-delay-bars", type=int, default=12)
    parser.add_argument("--maximum-notional-to-equity", type=float, default=8.0)
    parser.add_argument("--maximum-worst-drawdown", type=float, default=0.35)
    parser.add_argument("--bootstrap-simulations", type=int, default=10_000)
    parser.add_argument(
        "--phase",
        choices=("discovery", "holdout"),
        default="discovery",
        help=(
            "Discovery results can rank variants but cannot promote paper/live. "
            "Holdout requires --frozen-policy-sha."
        ),
    )
    parser.add_argument(
        "--frozen-policy-sha",
        default=None,
        help="Git commit/blob SHA that freezes all strategy and execution policy.",
    )
    namespace = parser.parse_args()
    global _LAST_ARGS
    _LAST_ARGS = namespace
    return namespace


def _build_authority_sets(
    book,
    *,
    v08_policy,
    intraday_policy,
):
    baseline_break_retest = tuple(
        authority
        for authority in build_baseline_event_authorities(book)
        if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
    )
    v05 = build_m15_m5_liquidity_delivery_result(book)
    leader = base._deduplicate_authorities(
        (*baseline_break_retest, *v05.authorities)
    )
    htf = build_v08_hardened_scene_family_result(
        book,
        costs=base.COSTS,
        policy=v08_policy,
    )
    intraday = build_v08_intraday_hardened_result(
        book,
        costs=base.COSTS,
        policy=intraday_policy,
    )
    sets = {
        "leader": leader,
        "leader_plus_v08_hardened_htf": base._deduplicate_authorities(
            (*leader, *htf.authorities)
        ),
        "leader_plus_v08_hardened_intraday": base._deduplicate_authorities(
            (*leader, *intraday.authorities)
        ),
        "leader_plus_all_v08_hardened": base._deduplicate_authorities(
            (*leader, *htf.authorities, *intraday.authorities)
        ),
    }
    diagnostics = {
        "v05": asdict(v05.diagnostics),
        "v08_hardened_htf": asdict(htf.diagnostics),
        "v08_hardened_intraday": asdict(intraday.diagnostics),
        "authority_counts": {name: len(items) for name, items in sets.items()},
    }
    return sets, diagnostics


def _trial_specs(summary: dict[str, object]) -> tuple[TrialSpec, ...]:
    manifests = summary["trial_manifests"]
    assert isinstance(manifests, dict)
    output: list[TrialSpec] = []
    for manifest in manifests.values():
        assert isinstance(manifest, dict)
        windows = tuple(
            EvaluationWindow(
                year=int(window["year"]),
                score_start=window["score_start"],
                score_end=window["score_end"],
                data_start=window["data_start"],
                data_end=window["data_end"],
            )
            for window in manifest["windows"]
        )
        output.append(TrialSpec(seed=int(manifest["seed"]), windows=windows))
    return tuple(sorted(output, key=lambda trial: trial.fingerprint))


def _enrich_summary(args: argparse.Namespace) -> None:
    path = args.output_dir / "summary.json"
    if not path.exists():
        return
    summary = json.loads(path.read_text(encoding="utf-8"))
    trials = _trial_specs(summary)
    overlap = summarize_trial_overlap(trials)
    minimum_trades = int(
        summary["contract"]["minimum_completed_trades_per_trial"]
    )
    minimum_feasibility = growth_feasibility(
        target_multiple=5.0,
        risk_fraction=args.risk_fraction,
        trades=minimum_trades,
    )

    arms = summary["arms"]
    for arm_summary in arms.values():
        robustness = arm_summary["robustness"]
        median_trades = max(1, math.floor(float(robustness["median_trades"])))
        arm_summary["growth_feasibility"] = {
            "at_minimum_trade_count": asdict(minimum_feasibility),
            "at_floor_median_trade_count": asdict(
                growth_feasibility(
                    target_multiple=5.0,
                    risk_fraction=args.risk_fraction,
                    trades=median_trades,
                )
            ),
        }
        promotion = evaluate_promotion_eligibility(
            phase=args.phase,
            economic_gate_passed=bool(arm_summary["passed"]),
            frozen_policy_sha=args.frozen_policy_sha,
            has_censored_trials=bool(arm_summary["invalid_trial_fingerprints"]),
        )
        arm_summary["promotion"] = asdict(promotion)

    best_arm = str(summary["best_arm"])
    summary["governance"] = {
        "phase": args.phase,
        "frozen_policy_sha": args.frozen_policy_sha,
        "trial_overlap": asdict(overlap),
        "minimum_trade_count_growth_feasibility": asdict(minimum_feasibility),
        "iid_trade_bootstrap_is_diagnostic_only": True,
        "paper_live_promotion_eligible": bool(
            arms[best_arm]["promotion"]["eligible"]
        ),
        "paper_live_authority": "RESEARCH_ONLY",
    }
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    base.ARMS = ARMS
    base._args = _args
    base._build_authority_sets = _build_authority_sets
    result = base.main()
    if _LAST_ARGS is not None:
        _enrich_summary(_LAST_ARGS)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
