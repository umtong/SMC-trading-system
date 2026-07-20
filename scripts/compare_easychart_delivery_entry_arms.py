from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from ictbt.easychart_v0.application import (
    HistoricalReplayRun,
    _first_opportunity_expiration,
    _pending_cancellation,
    intent_from_opportunity,
    load_5m_csv,
    run_historical_replay,
)
from ictbt.easychart_v0.domain import EntryMode, Timeframe
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    assemble_confluence_opportunities,
    build_feature_book,
)
from ictbt.easychart_v0.replay import replay_intent
from ictbt.easychart_v0.strategy import SimpleExecutionCosts

from analyze_easychart_v0_2_winrate import COSTS, WINDOWS


ARM_NEXT_OPEN = "event_created_next_bar_open"
ARM_FIRST_REVISIT = "event_created_first_revisit"
LEGACY_ARM = "legacy_v0_2_temporal_reference"
ARM_MODES = {
    ARM_NEXT_OPEN: EntryMode.NEXT_BAR_OPEN,
    ARM_FIRST_REVISIT: EntryMode.LIMIT_FIRST_REVISIT,
}


@dataclass(frozen=True, slots=True)
class WindowContext:
    index: int
    symbol: str
    environment: str
    start: pd.Timestamp
    end: pd.Timestamp
    tick_size: float
    candles: pd.DataFrame
    book: FeatureBook
    decision_times: tuple[pd.Timestamp, ...]


@dataclass(frozen=True, slots=True)
class ClosedTradeObservation:
    arm: str
    scope: str
    environment: str
    symbol: str
    authority_id: str
    entry_mode: str
    ob_causal_state: str
    side: str
    created_at: pd.Timestamp
    entry_time: pd.Timestamp
    closed_at: pd.Timestamp
    entry_price: float
    initial_stop: float
    initial_target: float
    target_r: float
    final_reason: str
    risk_budget: float
    net_pnl: float
    equity_before: float
    equity_after: float

    @property
    def net_r(self) -> float:
        return self.net_pnl / self.risk_budget

    def as_record(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "scope": self.scope,
            "environment": self.environment,
            "symbol": self.symbol,
            "authority_id": self.authority_id,
            "entry_mode": self.entry_mode,
            "ob_causal_state": self.ob_causal_state,
            "side": self.side,
            "created_at": self.created_at.isoformat(),
            "entry_time": self.entry_time.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "entry_price": self.entry_price,
            "initial_stop": self.initial_stop,
            "initial_target": self.initial_target,
            "target_r": self.target_r,
            "final_reason": self.final_reason,
            "risk_budget": self.risk_budget,
            "net_r": self.net_r,
            "net_pnl": self.net_pnl,
            "equity_before": self.equity_before,
            "equity_after": self.equity_after,
        }


@dataclass(frozen=True, slots=True)
class GlobalReplayResult:
    observations: tuple[ClosedTradeObservation, ...]
    initial_equity: float
    final_equity: float
    cancellations: int
    opportunity_rejections: int
    sizing_rejections: int
    entry_rejections: int
    open_censored: int
    entry_censored: int
    expired_before_submission: int
    simultaneous_candidate_cutoffs: int
    simultaneous_candidates: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the fixed EasyChart V0.2 legacy reference with the two "
            "event-created OB entry clocks. This is a fixed three-arm report, "
            "not a parameter sweep."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--legacy-dir",
        type=Path,
        default=Path("results/easychart_v0_2_scene_fidelity_final_fix/temporal"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_delivery_entry_comparison"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.01)
    parser.add_argument(
        "--window-index",
        type=int,
        help="Optional single-window smoke run. Global output then covers that window only.",
    )
    return parser.parse_args()


def _slice_window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    begin = pd.Timestamp(start, tz="UTC")
    finish = pd.Timestamp(end, tz="UTC")
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def _build_contexts(data_dir: Path, indices: Sequence[int]) -> tuple[WindowContext, ...]:
    selected = tuple(WINDOWS[index] for index in indices)
    source_frames = {
        symbol: load_5m_csv(data_dir / f"{symbol}_5m.csv")
        for symbol in {item[0] for item in selected}
    }
    contexts: list[WindowContext] = []
    for index in indices:
        symbol, environment, start, end, tick_size = WINDOWS[index]
        candles = _slice_window(source_frames[symbol], start, end)
        book = build_feature_book(candles, symbol=symbol, tick_size=tick_size)
        decision_times = tuple(
            sorted(
                {
                    block.known_at
                    for timeframe in (Timeframe.M5, Timeframe.M15)
                    for block in book.order_blocks[timeframe]
                }
            )
        )
        contexts.append(
            WindowContext(
                index=index,
                symbol=symbol,
                environment=environment,
                start=pd.Timestamp(start, tz="UTC"),
                end=pd.Timestamp(end, tz="UTC"),
                tick_size=tick_size,
                candles=candles,
                book=book,
                decision_times=decision_times,
            )
        )
    return tuple(contexts)


def _observation_from_attempt(
    *,
    arm: str,
    scope: str,
    environment: str,
    attempt,
) -> ClosedTradeObservation | None:
    trade = attempt.result.trade
    if trade is None:
        return None
    intent = attempt.intent
    return ClosedTradeObservation(
        arm=arm,
        scope=scope,
        environment=environment,
        symbol=intent.symbol,
        authority_id=attempt.authority_id,
        entry_mode=intent.entry_mode.value,
        ob_causal_state=intent.ob_causal_state.value,
        side=intent.side.value,
        created_at=intent.created_at,
        entry_time=trade.entry_time,
        closed_at=trade.closed_at,
        entry_price=trade.entry_price,
        initial_stop=trade.initial_stop,
        initial_target=trade.initial_target,
        target_r=trade.target_r,
        final_reason=trade.final_reason,
        risk_budget=intent.risk_budget,
        net_pnl=trade.net_pnl,
        equity_before=attempt.equity_before,
        equity_after=attempt.equity_after,
    )


def _max_drawdown(initial_equity: float, observations: Iterable[ClosedTradeObservation]) -> float:
    equity = float(initial_equity)
    peak = equity
    maximum = 0.0
    for observation in sorted(observations, key=lambda item: (item.closed_at, item.authority_id)):
        equity += observation.net_pnl
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _metrics(
    observations: Sequence[ClosedTradeObservation],
    *,
    initial_equity: float,
    cancellations: int = 0,
    opportunity_rejections: int = 0,
    sizing_rejections: int = 0,
    entry_rejections: int = 0,
    open_censored: int = 0,
    entry_censored: int = 0,
    expired_before_submission: int = 0,
    max_drawdown_override: float | None = None,
) -> dict[str, object]:
    wins = tuple(item for item in observations if item.net_pnl > 0)
    losses = tuple(item for item in observations if item.net_pnl < 0)
    gross_profit = sum(item.net_pnl for item in wins)
    gross_loss = -sum(item.net_pnl for item in losses)
    environments = {item.environment for item in observations}
    environment_pnl: dict[str, float] = defaultdict(float)
    for item in observations:
        environment_pnl[item.environment] += item.net_pnl
    return {
        "trades": len(observations),
        "wins": len(wins),
        "win_rate": len(wins) / len(observations) if observations else 0.0,
        "net_pnl": sum(item.net_pnl for item in observations),
        "expectancy_r": (
            sum(item.net_r for item in observations) / len(observations)
            if observations
            else 0.0
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "max_drawdown": (
            _max_drawdown(initial_equity, observations)
            if max_drawdown_override is None
            else max_drawdown_override
        ),
        "cancellations": cancellations,
        "opportunity_rejections": opportunity_rejections,
        "sizing_rejections": sizing_rejections,
        "entry_rejections": entry_rejections,
        "open_censored": open_censored,
        "entry_censored": entry_censored,
        "expired_before_submission": expired_before_submission,
        "traded_environments": len(environments),
        "positive_environments": sum(value > 0 for value in environment_pnl.values()),
        "environment_coverage": sorted(environments),
    }


def _run_metrics(
    run: HistoricalReplayRun,
    *,
    arm: str,
    environment: str,
) -> tuple[tuple[ClosedTradeObservation, ...], dict[str, object]]:
    observations = tuple(
        item
        for attempt in run.attempts
        if (
            item := _observation_from_attempt(
                arm=arm,
                scope="window",
                environment=environment,
                attempt=attempt,
            )
        )
        is not None
    )
    metrics = _metrics(
        observations,
        initial_equity=run.initial_equity,
        cancellations=len(run.pending_cancellations),
        opportunity_rejections=len(run.opportunity_rejections),
        sizing_rejections=len(run.sizing_rejections),
        entry_rejections=sum(
            attempt.result.status == "ENTRY_REJECTED" for attempt in run.attempts
        ),
        open_censored=sum(
            attempt.result.status == "OPEN_CENSORED" for attempt in run.attempts
        ),
        entry_censored=sum(
            attempt.result.status == "ENTRY_CENSORED" for attempt in run.attempts
        ),
        expired_before_submission=len(run.expired_before_submission),
    )
    metrics["initial_equity"] = run.initial_equity
    metrics["final_equity"] = run.final_equity
    return observations, metrics


def _run_window_panel(
    contexts: Sequence[WindowContext],
    *,
    arm: str,
    event_created_entry_mode: EntryMode,
    initial_equity: float,
    costs: CostConfig,
    risk: RiskConfig,
) -> tuple[tuple[ClosedTradeObservation, ...], dict[str, object]]:
    observations: list[ClosedTradeObservation] = []
    windows: list[dict[str, object]] = []
    maximum_window_drawdown = 0.0
    totals = defaultdict(int)
    for context in contexts:
        run = run_historical_replay(
            context.candles,
            symbol=context.symbol,
            tick_size=context.tick_size,
            equity=initial_equity,
            costs=costs,
            risk=risk,
            event_created_entry_mode=event_created_entry_mode,
        )
        window_observations, metrics = _run_metrics(
            run,
            arm=arm,
            environment=context.environment,
        )
        observations.extend(window_observations)
        maximum_window_drawdown = max(maximum_window_drawdown, float(metrics["max_drawdown"]))
        for name in (
            "cancellations",
            "opportunity_rejections",
            "sizing_rejections",
            "entry_rejections",
            "open_censored",
            "entry_censored",
            "expired_before_submission",
        ):
            totals[name] += int(metrics[name])
        windows.append(
            {
                "window_index": context.index,
                "symbol": context.symbol,
                "environment": context.environment,
                "start": context.start.isoformat(),
                "end": context.end.isoformat(),
                **metrics,
            }
        )
    aggregate = _metrics(
        observations,
        initial_equity=initial_equity,
        max_drawdown_override=maximum_window_drawdown,
        **totals,
    )
    aggregate["protocol"] = "six_independent_windows_each_reset_to_initial_equity"
    aggregate["max_drawdown_definition"] = "maximum cash drawdown among independent windows"
    return tuple(observations), {"aggregate": aggregate, "windows": windows}


def _authority_priority(result: Opportunity) -> tuple[object, ...]:
    authority = result.authority
    return (
        0
        if authority.location.timeframe is Timeframe.M15
        and authority.confirmation.timeframes == (Timeframe.M5,)
        else 1
        if authority.confirmation.timeframes == (Timeframe.M15,)
        else 2,
        0 if authority.has_literal_body_overlap else 1,
        authority.zone.width,
        -authority.known_at.value,
        result.symbol,
        authority.authority_id,
    )


def _run_global(
    contexts: Sequence[WindowContext],
    *,
    arm: str,
    event_created_entry_mode: EntryMode,
    initial_equity: float,
    costs: CostConfig,
    risk: RiskConfig,
) -> GlobalReplayResult:
    contexts_by_cutoff: dict[pd.Timestamp, list[WindowContext]] = defaultdict(list)
    for context in contexts:
        for cutoff in context.decision_times:
            contexts_by_cutoff[cutoff].append(context)

    target_costs = SimpleExecutionCosts(
        entry_fee_rate=costs.entry_fee_rate,
        exit_fee_rate=costs.target_fee_rate,
    )
    submitted_by_window: dict[int, set[str]] = {
        context.index: set() for context in contexts
    }
    current_equity = float(initial_equity)
    occupied_until: pd.Timestamp | None = None
    observations: list[ClosedTradeObservation] = []
    cancellations = 0
    opportunity_rejections = 0
    sizing_rejections = 0
    entry_rejections = 0
    open_censored = 0
    entry_censored = 0
    expired_before_submission = 0
    simultaneous_candidate_cutoffs = 0
    simultaneous_candidates = 0

    for cutoff in sorted(contexts_by_cutoff):
        if occupied_until is not None and cutoff < occupied_until:
            continue
        active_contexts = contexts_by_cutoff[cutoff]
        opportunities: list[tuple[Opportunity, WindowContext]] = []
        for context in active_contexts:
            results = assemble_confluence_opportunities(
                context.book,
                as_of=cutoff,
                costs=target_costs,
                excluded_authority_ids=frozenset(submitted_by_window[context.index]),
                event_created_entry_mode=event_created_entry_mode,
            )
            if not results:
                continue
            result = results[0]
            if isinstance(result, OpportunityRejection):
                submitted_by_window[context.index].add(result.authority_id)
                opportunity_rejections += 1
                continue
            expiration = _first_opportunity_expiration(context.book, result)
            if expiration is not None and expiration[0] <= cutoff:
                submitted_by_window[context.index].add(result.authority_id)
                expired_before_submission += 1
                continue
            opportunities.append((result, context))

        if len(opportunities) > 1:
            simultaneous_candidate_cutoffs += 1
            simultaneous_candidates += len(opportunities)
        if not opportunities:
            continue
        opportunities.sort(key=lambda item: _authority_priority(item[0]))

        selected: tuple[Opportunity, WindowContext] | None = None
        intent = None
        for opportunity, context in opportunities:
            submitted_by_window[context.index].add(opportunity.authority_id)
            try:
                intent = intent_from_opportunity(
                    opportunity,
                    equity=current_equity,
                    costs=costs,
                    risk=risk,
                    event_created_entry_mode=event_created_entry_mode,
                )
            except ValueError:
                sizing_rejections += 1
                continue
            selected = (opportunity, context)
            break
        if selected is None or intent is None:
            continue

        opportunity, context = selected
        equity_before = current_equity
        replay = replay_intent(
            intent,
            candles=context.candles,
            candle_interval="5min",
            costs=costs,
            volume_bars={
                Timeframe.M5: context.candles,
                Timeframe.M15: context.book.frames[Timeframe.M15],
            },
        )
        cancellation = _pending_cancellation(context.book, opportunity, replay)
        if cancellation is not None:
            cancellations += 1
            occupied_until = cancellation[0]
            continue
        if replay.status == "ENTRY_REJECTED":
            entry_rejections += 1
            occupied_until = replay.events[-1].occurred_at
            continue
        if replay.trade is not None:
            current_equity += replay.trade.net_pnl
            observation = _observation_from_attempt(
                arm=arm,
                scope="global",
                environment=context.environment,
                attempt=type(
                    "AttemptView",
                    (),
                    {
                        "result": replay,
                        "intent": intent,
                        "authority_id": opportunity.authority_id,
                        "equity_before": equity_before,
                        "equity_after": current_equity,
                    },
                )(),
            )
            assert observation is not None
            observations.append(observation)
            occupied_until = replay.trade.closed_at
            continue
        if replay.status == "OPEN_CENSORED":
            open_censored += 1
        else:
            entry_censored += 1
        # The representative windows contain deliberate gaps. A censored
        # pending/open state owns the shared slot through its supplied window,
        # then the next sampled environment resumes with the same cash equity.
        occupied_until = context.end

    return GlobalReplayResult(
        observations=tuple(observations),
        initial_equity=float(initial_equity),
        final_equity=current_equity,
        cancellations=cancellations,
        opportunity_rejections=opportunity_rejections,
        sizing_rejections=sizing_rejections,
        entry_rejections=entry_rejections,
        open_censored=open_censored,
        entry_censored=entry_censored,
        expired_before_submission=expired_before_submission,
        simultaneous_candidate_cutoffs=simultaneous_candidate_cutoffs,
        simultaneous_candidates=simultaneous_candidates,
    )


def _legacy_reference(
    legacy_dir: Path,
    *,
    indices: Sequence[int],
    initial_equity: float,
    risk_fraction: float,
) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    windows: list[dict[str, object]] = []
    ledger: list[dict[str, object]] = []
    observations: list[ClosedTradeObservation] = []
    maximum_window_drawdown = 0.0
    total_cancellations = 0
    total_rejections = 0
    total_open_censored = 0
    total_entry_censored = 0
    for index in indices:
        summary_path = legacy_dir / f"summary_{index}.json"
        records_path = legacy_dir / f"records_{index}.csv"
        if not summary_path.exists() or not records_path.exists():
            raise FileNotFoundError(
                f"fixed legacy artifact is missing: {summary_path} or {records_path}"
            )
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        window_summary = payload["windows"][0]
        records = pd.read_csv(records_path)
        closed = records.loc[records["status"] == "closed"].copy()
        closed["entry_time"] = pd.to_datetime(closed["entry_time"], utc=True)
        closed["closed_at"] = pd.to_datetime(closed["closed_at"], utc=True)
        closed = closed.sort_values(["entry_time", "authority_id"])
        symbol = WINDOWS[index][0]
        equity = float(initial_equity)
        window_observations: list[ClosedTradeObservation] = []
        for row in closed.itertuples(index=False):
            risk_budget = equity * risk_fraction
            net_pnl = float(row.net_pnl)
            observation = ClosedTradeObservation(
                arm=LEGACY_ARM,
                scope="window",
                environment=str(row.environment),
                symbol=symbol,
                authority_id=str(row.authority_id),
                entry_mode=EntryMode.LIMIT_FIRST_REVISIT.value,
                ob_causal_state="legacy_unclassified",
                side=str(row.side),
                created_at=pd.Timestamp(row.created_at),
                entry_time=pd.Timestamp(row.entry_time),
                closed_at=pd.Timestamp(row.closed_at),
                entry_price=float(row.entry_price),
                initial_stop=float(row.initial_stop),
                initial_target=float(row.initial_target),
                target_r=float(row.target_r),
                final_reason=str(row.final_reason),
                risk_budget=risk_budget,
                net_pnl=net_pnl,
                equity_before=equity,
                equity_after=equity + net_pnl,
            )
            equity += net_pnl
            observations.append(observation)
            window_observations.append(observation)
            ledger.append(observation.as_record())
        cancellations = int(window_summary["cancellations"])
        rejections = int(window_summary["rejections"])
        open_censored = int(window_summary.get("open_censored", 0))
        entry_censored = int(window_summary.get("entry_censored", 0))
        total_cancellations += cancellations
        total_rejections += rejections
        total_open_censored += open_censored
        total_entry_censored += entry_censored
        metrics = _metrics(
            window_observations,
            initial_equity=initial_equity,
            cancellations=cancellations,
            opportunity_rejections=rejections,
            open_censored=open_censored,
            entry_censored=entry_censored,
        )
        maximum_window_drawdown = max(maximum_window_drawdown, float(metrics["max_drawdown"]))
        metrics["initial_equity"] = initial_equity
        metrics["final_equity"] = equity
        windows.append(
            {
                "window_index": index,
                "symbol": symbol,
                "environment": window_summary["environment"],
                "source_summary": str(summary_path),
                **metrics,
            }
        )
    aggregate = _metrics(
        observations,
        initial_equity=initial_equity,
        cancellations=total_cancellations,
        opportunity_rejections=total_rejections,
        open_censored=total_open_censored,
        entry_censored=total_entry_censored,
        max_drawdown_override=maximum_window_drawdown,
    )
    aggregate.update(
        {
            "protocol": "fixed_legacy_artifacts_independent_windows",
            "global_portfolio_available": False,
            "max_drawdown_definition": "maximum cash drawdown among independent windows",
            "expectancy_r_note": (
                "Reconstructed as net PnL divided by 1% of pre-trade window equity; "
                "the legacy CSV did not preserve intent.risk_budget."
            ),
        }
    )
    return windows, aggregate, ledger


def main() -> int:
    args = _parse_args()
    if args.initial_equity <= 0:
        raise ValueError("initial-equity must be positive")
    if not 0 < args.risk_fraction <= 1:
        raise ValueError("risk-fraction must be in (0, 1]")
    if not math.isclose(args.risk_fraction, 0.01, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("fixed legacy cash comparison requires risk-fraction 0.01")
    if not math.isclose(args.initial_equity, 10_000.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("fixed legacy cash comparison requires initial-equity 10000")
    if args.window_index is None:
        indices = tuple(range(len(WINDOWS)))
    else:
        if not 0 <= args.window_index < len(WINDOWS):
            raise ValueError("window-index is out of range")
        indices = (args.window_index,)

    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    contexts = _build_contexts(args.data_dir, indices)
    legacy_windows, legacy_aggregate, legacy_ledger = _legacy_reference(
        args.legacy_dir,
        indices=indices,
        initial_equity=args.initial_equity,
        risk_fraction=args.risk_fraction,
    )

    ledger = list(legacy_ledger)
    window_panels: dict[str, object] = {
        LEGACY_ARM: {"aggregate": legacy_aggregate, "windows": legacy_windows}
    }
    global_panels: dict[str, object] = {}
    for arm, mode in ARM_MODES.items():
        arm_observations, panel = _run_window_panel(
            contexts,
            arm=arm,
            event_created_entry_mode=mode,
            initial_equity=args.initial_equity,
            costs=COSTS,
            risk=risk,
        )
        ledger.extend(item.as_record() for item in arm_observations)
        window_panels[arm] = panel

        global_result = _run_global(
            contexts,
            arm=arm,
            event_created_entry_mode=mode,
            initial_equity=args.initial_equity,
            costs=COSTS,
            risk=risk,
        )
        ledger.extend(item.as_record() for item in global_result.observations)
        metrics = _metrics(
            global_result.observations,
            initial_equity=global_result.initial_equity,
            cancellations=global_result.cancellations,
            opportunity_rejections=global_result.opportunity_rejections,
            sizing_rejections=global_result.sizing_rejections,
            entry_rejections=global_result.entry_rejections,
            open_censored=global_result.open_censored,
            entry_censored=global_result.entry_censored,
            expired_before_submission=global_result.expired_before_submission,
        )
        metrics.update(
            {
                "initial_equity": global_result.initial_equity,
                "final_equity": global_result.final_equity,
                "protocol": "chronological_single_equity_single_pending_or_open_slot",
                "simultaneous_candidate_cutoffs": global_result.simultaneous_candidate_cutoffs,
                "simultaneous_candidates": global_result.simultaneous_candidates,
                "tie_break": "authority_priority_fields_then_symbol_then_authority_id",
                "sample_gap_policy": (
                    "censored pending/open state owns the slot through its supplied "
                    "window; the next sampled environment resumes with unchanged cash equity"
                ),
            }
        )
        global_panels[arm] = metrics

    payload = {
        "strategy_version": "easychart_ob_v0_3_m15_event_m5_delivery",
        "comparison_type": "fixed_three_arm_no_parameter_sweep",
        "initial_equity": args.initial_equity,
        "risk_fraction": args.risk_fraction,
        "costs": {
            "entry_fee_rate": COSTS.entry_fee_rate,
            "stop_fee_rate": COSTS.stop_fee_rate,
            "target_fee_rate": COSTS.target_fee_rate,
            "volume_exit_fee_rate": COSTS.volume_exit_fee_rate,
            "stop_slippage_bps": COSTS.stop_slippage_bps,
            "volume_exit_slippage_bps": COSTS.volume_exit_slippage_bps,
        },
        "window_indices": list(indices),
        "window_panel": window_panels,
        "global_portfolio": global_panels,
        "legacy_global_note": (
            "The fixed 59-trade legacy artifact reset equity per window and did not "
            "preserve the unselected opportunity stream, so it is not relabelled as a "
            "global single-slot replay."
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pd.DataFrame.from_records(ledger).to_csv(
        args.output_dir / "trade_ledger.csv", index=False
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
