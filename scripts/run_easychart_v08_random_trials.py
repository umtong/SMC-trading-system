from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import math
from pathlib import Path
from typing import Iterable

import pandas as pd

from ictbt.easychart_v0.application import load_5m_csv
from ictbt.easychart_v0.domain import (
    B1Subtype,
    ConfluenceAuthority,
    LiquidityDeliveryAuthority,
    Side,
)
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    assemble_opportunity as assemble_v03_opportunity,
    build_feature_book,
)
from ictbt.easychart_v0.portfolio import (
    PortfolioContext,
    PortfolioReplayResult,
    run_global_portfolio,
)
from ictbt.easychart_v0.research_protocol import (
    GrowthGate,
    TrialPerformance,
    YearCoverage,
    bootstrap_path_stress,
    evaluate_growth_gate,
    manifests_by_fingerprint,
    sample_trials,
)
from ictbt.easychart_v0.strategy import SimpleExecutionCosts
from ictbt.easychart_v0.v04 import (
    assemble_v04_opportunity,
    build_baseline_event_authorities,
)
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result
from ictbt.easychart_v0.v07 import SrFlipFvgAuthority
from ictbt.easychart_v0.v08 import (
    V08Policy,
    assemble_v08_opportunity,
    build_v08_scene_family_result,
)
from ictbt.easychart_v0.v08_intraday import (
    V08IntradayPolicy,
    build_v08_intraday_liquidity_delivery_result,
)


SYMBOLS = ("BTCUSDT", "ETHUSDT")
TICK_SIZES = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}
ARMS = (
    "leader",
    "leader_plus_v08_htf",
    "leader_plus_v08_intraday",
    "leader_plus_all_v08",
)


COSTS = CostConfig(
    entry_fee_rate=0.0002,
    stop_fee_rate=0.0006,
    target_fee_rate=0.0002,
    volume_exit_fee_rate=0.0006,
    stop_slippage_bps=2.0,
    volume_exit_slippage_bps=2.0,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repeated 2022-2026 random four-week EasyChart/SMC portfolio research"
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v08_random_trials"),
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
    return parser.parse_args()


def _coverage_intersection(
    frames: dict[str, pd.DataFrame],
    *,
    years: Iterable[int] = range(2022, 2027),
) -> tuple[YearCoverage, ...]:
    first = max(frame.index[0] for frame in frames.values())
    end = min(frame.index[-1] + pd.Timedelta(minutes=5) for frame in frames.values())
    output: list[YearCoverage] = []
    for year in years:
        year_start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        year_end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
        start = max(first.normalize(), year_start)
        finish = min(end.normalize(), year_end)
        output.append(YearCoverage(year, start, finish))
    return tuple(output)


def _slice_window(
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    selected = frame.loc[(frame.index >= start) & (frame.index < end)].copy()
    expected = int((end - start) / pd.Timedelta(minutes=5))
    if len(selected) != expected:
        raise ValueError(
            f"window {start.isoformat()}..{end.isoformat()} is not contiguous: "
            f"expected {expected} bars, found {len(selected)}"
        )
    return selected


def _semantic_key(authority: object) -> tuple[object, ...]:
    if isinstance(authority, LiquidityDeliveryAuthority):
        return (
            "liquidity_delivery",
            authority.known_at,
            authority.side,
            authority.delivery_root_id,
        )
    if isinstance(authority, SrFlipFvgAuthority):
        return ("sr_flip", authority.scene_root_id)
    if isinstance(authority, ConfluenceAuthority):
        return (
            "confluence",
            authority.known_at,
            authority.side,
            authority.confirmation.authority_id,
        )
    return (authority.known_at, authority.side, authority.authority_id)


def _deduplicate_authorities(authorities: Iterable[object]) -> tuple[object, ...]:
    grouped: dict[tuple[object, ...], list[object]] = {}
    for authority in authorities:
        grouped.setdefault(_semantic_key(authority), []).append(authority)
    selected = [
        min(
            items,
            key=lambda authority: (
                0 if str(authority.authority_id).startswith("v08-") else 1,
                0 if bool(getattr(authority, "has_literal_body_overlap", False)) else 1,
                float(authority.zone.width),
                -authority.known_at.value,
                authority.authority_id,
            ),
        )
        for items in grouped.values()
    ]
    return tuple(
        sorted(selected, key=lambda authority: (authority.known_at, authority.authority_id))
    )


def _build_authority_sets(
    book: FeatureBook,
    *,
    v08_policy: V08Policy,
    intraday_policy: V08IntradayPolicy,
) -> tuple[dict[str, tuple[object, ...]], dict[str, object]]:
    baseline_break_retest = tuple(
        authority
        for authority in build_baseline_event_authorities(book)
        if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
    )
    v05 = build_m15_m5_liquidity_delivery_result(book)
    leader = _deduplicate_authorities((*baseline_break_retest, *v05.authorities))

    v08_htf = build_v08_scene_family_result(
        book,
        costs=COSTS,
        policy=v08_policy,
    )
    v08_intraday = build_v08_intraday_liquidity_delivery_result(
        book,
        costs=COSTS,
        policy=intraday_policy,
    )
    sets = {
        "leader": leader,
        "leader_plus_v08_htf": _deduplicate_authorities(
            (*leader, *v08_htf.authorities)
        ),
        "leader_plus_v08_intraday": _deduplicate_authorities(
            (*leader, *v08_intraday.authorities)
        ),
        "leader_plus_all_v08": _deduplicate_authorities(
            (*leader, *v08_htf.authorities, *v08_intraday.authorities)
        ),
    }
    diagnostics = {
        "v05": asdict(v05.diagnostics),
        "v08_htf": asdict(v08_htf.diagnostics),
        "v08_intraday": asdict(v08_intraday.diagnostics),
        "authority_counts": {name: len(items) for name, items in sets.items()},
    }
    return sets, diagnostics


def _assemble_candidate(
    book: FeatureBook,
    authority: object,
    costs: CostConfig,
) -> Opportunity | OpportunityRejection:
    if isinstance(authority, SrFlipFvgAuthority):
        return assemble_v08_opportunity(book, authority, costs=costs)
    if isinstance(authority, ConfluenceAuthority) and authority.destination is None:
        return assemble_v03_opportunity(
            book,
            authority,
            as_of=authority.known_at,
            costs=SimpleExecutionCosts(
                entry_fee_rate=costs.entry_fee_rate,
                exit_fee_rate=costs.target_fee_rate,
            ),
            event_created_entry_mode="limit_first_revisit",
        )
    return assemble_v04_opportunity(book, authority, costs=costs)


def _trade_row(
    *,
    arm: str,
    trial_fingerprint: str,
    closed,
) -> dict[str, object]:
    trade = closed.attempt.result.trade
    assert trade is not None
    authority = closed.authority
    return {
        "arm": arm,
        "trial_fingerprint": trial_fingerprint,
        "context_id": closed.context.context_id,
        "symbol": closed.context.symbol,
        "authority_id": authority.authority_id,
        "scene_family": trade.scene_family.value,
        "side": trade.side.value,
        "entry_time": trade.entry_time.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "target_r": trade.target_r,
        "net_r": trade.net_pnl / closed.attempt.intent.risk_budget,
        "net_pnl": trade.net_pnl,
        "equity_before": closed.attempt.equity_before,
        "equity_after": closed.attempt.equity_after,
        "final_reason": trade.final_reason,
        "source_strategy": (
            "v08_htf"
            if isinstance(authority, SrFlipFvgAuthority)
            else "v08_intraday"
            if str(authority.authority_id).startswith(
                "v08-internal-liquidity-delivery:"
            )
            else "leader"
        ),
    }


def _trial_summary(
    *,
    arm: str,
    fingerprint: str,
    result: PortfolioReplayResult,
) -> dict[str, object]:
    return {
        "arm": arm,
        "trial_fingerprint": fingerprint,
        "valid": result.valid,
        "initial_equity": result.initial_equity,
        "final_equity": result.final_equity,
        "equity_multiple": result.equity_multiple,
        "trades": result.trades,
        "operating_days": result.operating_days,
        "trades_per_operating_day": result.trades_per_operating_day,
        "trade_surplus_over_operating_days": result.trades - result.operating_days,
        "wins": result.wins,
        "win_rate": None if result.trades == 0 else result.wins / result.trades,
        "net_r": result.net_r,
        "average_net_r": result.average_net_r,
        "max_drawdown_fraction": result.max_drawdown_fraction,
        "opportunity_rejections": result.opportunity_rejections,
        "sizing_rejections": result.sizing_rejections,
        "pending_cancellations": result.pending_cancellations,
        "entry_rejections": result.entry_rejections,
        "open_censored": result.open_censored,
        "entry_censored": result.entry_censored,
        "slot_suppressed_authorities": result.slot_suppressed_authorities,
        "simultaneous_candidate_cutoffs": result.simultaneous_candidate_cutoffs,
        "simultaneous_candidates": result.simultaneous_candidates,
    }


def main() -> int:
    args = _args()
    selected_arms = tuple(args.arm or ARMS)
    if args.trials <= 0:
        raise SystemExit("trials must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    frames = {
        symbol: load_5m_csv(args.data_dir / f"{symbol}_5m.csv")
        for symbol in SYMBOLS
    }
    coverages = _coverage_intersection(frames)
    trials = sample_trials(
        coverages,
        trial_count=args.trials,
        seed=args.seed,
        score_days=args.score_days_per_year,
        warmup_days=args.warmup_days,
        exit_extension_days=args.exit_extension_days,
    )
    expected_operating_days = args.score_days_per_year * len(coverages)
    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    v08_policy = V08Policy(
        minimum_target_r=args.v08_minimum_target_r,
        minimum_displacement_range_multiple=args.v08_displacement_multiple,
        risk_fraction=args.risk_fraction,
        maximum_notional_to_equity=args.maximum_notional_to_equity,
    )
    intraday_policy = V08IntradayPolicy(
        maximum_delivery_delay_bars=args.intraday_maximum_delay_bars,
        minimum_target_r=args.intraday_minimum_target_r,
        minimum_displacement_range_multiple=(
            args.intraday_displacement_multiple
        ),
        risk_fraction=args.risk_fraction,
        maximum_notional_to_equity=args.maximum_notional_to_equity,
    )

    trial_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    diagnostic_rows: list[dict[str, object]] = []
    performances: dict[str, list[TrialPerformance]] = {
        arm: [] for arm in selected_arms
    }
    invalid_trials: dict[str, list[str]] = {arm: [] for arm in selected_arms}

    for ordinal, trial in enumerate(trials, start=1):
        print(
            f"[{ordinal}/{len(trials)}] trial={trial.fingerprint}",
            flush=True,
        )
        contexts_by_arm: dict[str, list[PortfolioContext]] = {
            arm: [] for arm in selected_arms
        }
        for window in trial.windows:
            for symbol in SYMBOLS:
                candles = _slice_window(
                    frames[symbol],
                    start=window.data_start,
                    end=window.data_end,
                )
                book = build_feature_book(
                    candles,
                    symbol=symbol,
                    tick_size=TICK_SIZES[symbol],
                )
                authority_sets, diagnostics = _build_authority_sets(
                    book,
                    v08_policy=v08_policy,
                    intraday_policy=intraday_policy,
                )
                context_id = (
                    f"{trial.fingerprint}:{window.year}:{symbol}:"
                    f"{window.score_start.date().isoformat()}"
                )
                diagnostic_rows.append(
                    {
                        "trial_fingerprint": trial.fingerprint,
                        "year": window.year,
                        "symbol": symbol,
                        "score_start": window.score_start.isoformat(),
                        "score_end": window.score_end.isoformat(),
                        **{
                            f"{family}_{name}": value
                            for family, values in diagnostics.items()
                            if family != "authority_counts"
                            for name, value in values.items()
                        },
                        **{
                            f"authorities_{name}": value
                            for name, value in diagnostics[
                                "authority_counts"
                            ].items()
                        },
                    }
                )
                for arm in selected_arms:
                    contexts_by_arm[arm].append(
                        PortfolioContext(
                            context_id=context_id,
                            symbol=symbol,
                            candles=candles,
                            book=book,
                            authorities=authority_sets[arm],
                            score_start=window.score_start,
                            score_end=window.score_end,
                            data_end=window.data_end,
                        )
                    )

        for arm in selected_arms:
            result = run_global_portfolio(
                contexts_by_arm[arm],
                initial_equity=args.initial_equity,
                costs=COSTS,
                risk=risk,
                assemble_candidate=_assemble_candidate,
            )
            if result.operating_days != expected_operating_days:
                raise RuntimeError("portfolio operating-day denominator is wrong")
            trial_rows.append(
                _trial_summary(
                    arm=arm,
                    fingerprint=trial.fingerprint,
                    result=result,
                )
            )
            trade_rows.extend(
                _trade_row(
                    arm=arm,
                    trial_fingerprint=trial.fingerprint,
                    closed=closed,
                )
                for closed in result.closed_attempts
            )
            performances[arm].append(
                TrialPerformance(
                    trial_fingerprint=trial.fingerprint,
                    initial_equity=result.initial_equity,
                    final_equity=result.final_equity,
                    max_drawdown_fraction=result.max_drawdown_fraction,
                    trades=result.trades,
                    wins=result.wins,
                    net_r=result.net_r,
                    operating_days=result.operating_days,
                    average_net_r=result.average_net_r,
                )
            )
            if not result.valid:
                invalid_trials[arm].append(trial.fingerprint)
            print(
                f"  {arm}: valid={result.valid} trades={result.trades} "
                f"multiple={result.equity_multiple:.3f} "
                f"dd={result.max_drawdown_fraction:.2%}",
                flush=True,
            )

    gate = GrowthGate(
        target_multiple=5.0,
        required_target_hit_rate=1.0,
        minimum_trials=args.trials,
        minimum_trade_surplus_over_operating_days=1,
        maximum_worst_drawdown_fraction=args.maximum_worst_drawdown,
        minimum_median_average_net_r=0.0,
    )
    arm_summaries: dict[str, object] = {}
    for arm in selected_arms:
        gate_result = evaluate_growth_gate(performances[arm], gate=gate)
        reasons = list(gate_result.reasons)
        if invalid_trials[arm]:
            reasons.append("censored_trial")
        net_rs = [
            float(row["net_r"])
            for row in trade_rows
            if row["arm"] == arm
        ]
        stress = (
            None
            if not net_rs
            else bootstrap_path_stress(
                net_rs,
                risk_fraction=args.risk_fraction,
                simulations=args.bootstrap_simulations,
                trades_per_path=expected_operating_days + 1,
                seed=args.seed,
            )
        )
        arm_summaries[arm] = {
            "passed": gate_result.passed and not invalid_trials[arm],
            "reasons": reasons,
            "robustness": asdict(gate_result.summary),
            "invalid_trial_fingerprints": invalid_trials[arm],
            "path_stress": None if stress is None else asdict(stress),
        }

    ranking = sorted(
        selected_arms,
        key=lambda arm: (
            -int(bool(arm_summaries[arm]["passed"])),
            -float(
                arm_summaries[arm]["robustness"][
                    "minimum_trade_surplus_over_operating_days"
                ]
            ),
            -float(
                arm_summaries[arm]["robustness"][
                    "worst_equity_multiple"
                ]
            ),
            -float(
                arm_summaries[arm]["robustness"][
                    "median_equity_multiple"
                ]
            ),
            float(
                arm_summaries[arm]["robustness"][
                    "worst_max_drawdown_fraction"
                ]
            ),
        ),
    )
    summary = {
        "contract": {
            "years": [coverage.year for coverage in coverages],
            "trials": args.trials,
            "seed": args.seed,
            "score_days_per_year": args.score_days_per_year,
            "portfolio_operating_days_per_trial": expected_operating_days,
            "minimum_completed_trades_per_trial": expected_operating_days + 1,
            "trade_frequency_rule": (
                "completed trades must be strictly greater than unique "
                "portfolio operating days; pending/cancelled/rejected intents do not count"
            ),
            "target_equity_multiple": 5.0,
            "risk_fraction": args.risk_fraction,
            "one_total_slot_btc_eth": True,
            "daily_loss_limit_enabled": False,
            "initial_equity": args.initial_equity,
            "costs": asdict(COSTS),
            "v08_policy": asdict(v08_policy),
            "v08_intraday_policy": asdict(intraday_policy),
            "coverage": [
                {
                    "year": coverage.year,
                    "available_start": coverage.available_start.isoformat(),
                    "available_end": coverage.available_end.isoformat(),
                }
                for coverage in coverages
            ],
        },
        "trial_manifests": manifests_by_fingerprint(trials),
        "arms": arm_summaries,
        "ranking": ranking,
        "best_arm": ranking[0],
        "failure_reason_counts": {
            arm: dict(Counter(arm_summaries[arm]["reasons"]))
            for arm in selected_arms
        },
    }

    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(trial_rows).to_csv(
        args.output_dir / "trial_results.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(trade_rows).to_csv(
        args.output_dir / "trade_ledger.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(diagnostic_rows).to_csv(
        args.output_dir / "build_diagnostics.csv",
        index=False,
        encoding="utf-8-sig",
    )

    best = arm_summaries[ranking[0]]
    print(
        json.dumps(
            {
                "best_arm": ranking[0],
                "passed": best["passed"],
                "reasons": best["reasons"],
                "robustness": best["robustness"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if best["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
