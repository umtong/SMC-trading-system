from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd

from ictbt.easychart_v0.application import intent_from_opportunity
from ictbt.easychart_v0.domain import (
    B1Subtype,
    ConfluenceAuthority,
    EntryMode,
    LiquidityDeliveryAuthority,
    SceneFamily,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    assemble_opportunity as assemble_v03_opportunity,
    build_feature_book,
)
from ictbt.easychart_v0.replay import replay_intent
from ictbt.easychart_v0.strategy import SimpleExecutionCosts, select_initial_target
from ictbt.easychart_v0.v04 import (
    _preentry_expiration,
    assemble_v04_opportunity,
    build_baseline_event_authorities,
    run_v04_historical_replay,
)
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result



def _load_benchmark_contract() -> tuple[object, tuple[tuple[object, ...], ...]]:
    path = Path(__file__).resolve().with_name("analyze_easychart_v0_2_winrate.py")
    spec = importlib.util.spec_from_file_location(
        "easychart_v07_benchmark_contract",
        path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load benchmark contract from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.COSTS, tuple(module.WINDOWS)


COSTS, WINDOWS = _load_benchmark_contract()


ARMS = (
    "A_LEADER_V03_BREAK_RETEST_PLUS_V05",
    "B_V07_FIRST_RETURN_LIMIT",
    "C_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
    "D_LEADER_PLUS_V07_FIRST_RETURN_LIMIT",
    "E_LEADER_PLUS_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
)

FIRST_RETURN_LIMIT = "first_return_limit"
BOUNDARY_ACCEPT_NEXT_OPEN = "boundary_accept_next_open"


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
    leader: tuple[object, ...]
    v07: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class GlobalClosedAttempt:
    context: WindowContext
    authority: object
    attempt: object


@dataclass(frozen=True, slots=True)
class GlobalReplayResult:
    closed_attempts: tuple[GlobalClosedAttempt, ...]
    initial_equity: float
    final_equity: float
    opportunity_rejections: int
    sizing_rejections: int
    pending_cancellations: int
    entry_rejections: int
    open_censored: int
    entry_censored: int
    slot_suppressed_authorities: int
    simultaneous_candidate_cutoffs: int
    simultaneous_candidates: int


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v07_scene_families"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    parser.add_argument("--window-index", type=int)
    return parser.parse_args()


def _load_v07_api(
) -> tuple[Callable[..., object], Callable[..., object], Callable[..., object]]:
    """Load the V0.7 boundary only when the comparison is actually run.

    Keeping this import local lets the metric helpers and their tests remain
    usable while the independently developed V0.7 scene module is landing.
    The public contract is intentionally narrow: one builder result exposing
    ``authorities``/``diagnostics`` and one replay function accepting an
    explicit ``entry_arm``.
    """

    try:
        from ictbt.easychart_v0.v07 import (
            assemble_v07_opportunity,
            build_v07_scene_family_result,
            run_v07_historical_replay,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise SystemExit(
            "V0.7 API is not available: expected "
            "build_v07_scene_family_result(book) and "
            "assemble_v07_opportunity(book, authority, ...) and "
            "run_v07_historical_replay(..., entry_arm=...)"
        ) from exc
    return (
        build_v07_scene_family_result,
        assemble_v07_opportunity,
        run_v07_historical_replay,
    )


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("window boundary must be a valid timestamp")
    if timestamp.tz is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _dates_in_half_open_window(start: object, end: object) -> frozenset[date]:
    """Return UTC calendar dates touched by the half-open interval [start,end)."""

    begin = _utc(start)
    finish = _utc(end)
    if finish <= begin:
        raise ValueError("window end must follow window start")
    final_day = (finish - pd.Timedelta(nanoseconds=1)).normalize()
    return frozenset(
        timestamp.date()
        for timestamp in pd.date_range(
            begin.normalize(),
            final_day,
            freq="1D",
            tz="UTC",
        )
    )


def _instrument_days(windows: Iterable[Sequence[object]]) -> int:
    """Keep the existing panel denominator: count each symbol-window day."""

    return sum(
        len(_dates_in_half_open_window(window[2], window[3]))
        for window in windows
    )


def _portfolio_operating_dates(
    windows: Iterable[Sequence[object]],
) -> frozenset[date]:
    """Count each UTC operating date once even if several symbols overlap."""

    dates: set[date] = set()
    for window in windows:
        dates.update(_dates_in_half_open_window(window[2], window[3]))
    return frozenset(dates)


def _load_source(path: Path) -> pd.DataFrame:
    source = pd.read_csv(path)
    timestamp_column = next(
        name
        for name in ("open_time", "timestamp", "time", "datetime")
        if name in source.columns
    )
    source.index = pd.DatetimeIndex(
        pd.to_datetime(source.pop(timestamp_column), utc=True),
        name="open_time",
    )
    return source


def _window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    begin = _utc(start)
    finish = _utc(end)
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _mfe_r(candles: pd.DataFrame, trade: object) -> float:
    entry_price = float(getattr(trade, "entry_price"))
    initial_stop = float(getattr(trade, "initial_stop"))
    risk_distance = abs(entry_price - initial_stop)
    if risk_distance <= 0:
        return 0.0
    bars = candles.loc[
        (candles.index >= getattr(trade, "entry_time"))
        & (candles.index <= getattr(trade, "closed_at"))
    ]
    if bars.empty:
        return 0.0
    side = _enum_value(getattr(trade, "side"))
    favorable = (
        float(bars["high"].max()) - entry_price
        if side == "long"
        else entry_price - float(bars["low"].min())
    )
    return max(0.0, favorable / risk_distance)


def _trade_row(
    *,
    arm: str,
    scope: str,
    entry_arm: str,
    window_index: int,
    symbol: str,
    environment: str,
    candles: pd.DataFrame,
    attempt: object,
    authority: object,
) -> dict[str, object]:
    result = getattr(attempt, "result")
    trade = getattr(result, "trade")
    assert trade is not None
    intent = getattr(attempt, "intent")
    authority_id = str(getattr(attempt, "authority_id"))
    subtype: object | None = None
    if isinstance(authority, ConfluenceAuthority):
        subtype = authority.confirmation.subtype.value
    elif isinstance(authority, LiquidityDeliveryAuthority):
        subtype = authority.delivery_kind
    else:
        subtype = _enum_value(
            getattr(
                authority,
                "event_kind",
                getattr(authority, "subtype", None),
            )
        )
    is_v07_scene = trade.scene_family is SceneFamily.SR_FLIP_FVG
    boundary = (
        getattr(authority, "boundary_pivot", None) if is_v07_scene else None
    )
    fvg = getattr(authority, "fvg", None) if is_v07_scene else None
    destination = (
        getattr(authority, "destination", None) if is_v07_scene else None
    )
    stop_distance = abs(float(trade.entry_price) - float(trade.initial_stop))
    original_quantity = float(trade.original_quantity)
    return {
        "arm": arm,
        "scope": scope,
        "entry_arm": entry_arm,
        "window_index": window_index,
        "symbol": symbol,
        "environment": environment,
        "source_strategy": "v07" if is_v07_scene else "leader",
        "scene_family": _enum_value(trade.scene_family),
        "subtype": subtype,
        "footprint_kind": _enum_value(
            getattr(authority, "footprint_kind", None)
        ),
        "stop_owner": _enum_value(getattr(authority, "stop_owner", None)),
        "authority_id": authority_id,
        "scene_root_id": getattr(authority, "scene_root_id", None),
        "location_id": getattr(authority, "location_id", None),
        "boundary_timeframe": _enum_value(
            getattr(boundary, "timeframe", None)
        ),
        "boundary_price": getattr(boundary, "price", None),
        "fvg_id": getattr(fvg, "fvg_id", None),
        "target_kind": _enum_value(getattr(destination, "kind", None)),
        "target_source_id": getattr(destination, "source_id", None),
        "side": _enum_value(trade.side),
        "intent_entry_mode": _enum_value(intent.entry_mode),
        "created_at": intent.created_at.isoformat(),
        "entry_time": trade.entry_time.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "stop_distance": stop_distance,
        "stop_distance_pct": stop_distance / trade.entry_price,
        "target_distance": abs(trade.initial_target - trade.entry_price),
        "target_r": trade.target_r,
        "mfe_r": _mfe_r(candles, trade),
        "partial_enabled": trade.target_r >= 1.4,
        "partial_1r": any(
            leg.reason == "partial_1r" for leg in trade.exit_legs
        ),
        "final_reason": trade.final_reason,
        "risk_budget": intent.risk_budget,
        "position_notional": original_quantity * trade.entry_price,
        "notional_to_equity": (
            original_quantity * trade.entry_price / attempt.equity_before
        ),
        "net_r": trade.net_pnl / intent.risk_budget,
        "net_pnl": trade.net_pnl,
        "equity_before": attempt.equity_before,
        "equity_after": attempt.equity_after,
    }


def _panel_max_drawdown_fraction(
    rows: list[dict[str, object]],
    initial_equity: float,
) -> float:
    worst = 0.0
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["window_index"])].append(row)
    for trades in grouped.values():
        peak = initial_equity
        for row in sorted(trades, key=lambda item: str(item["closed_at"])):
            equity = float(row["equity_after"])
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
    return worst


def _global_max_drawdown_fraction(
    rows: list[dict[str, object]],
    initial_equity: float,
) -> float:
    """Drawdown on one chronological shared-equity path."""

    peak = initial_equity
    worst = 0.0
    for row in sorted(rows, key=lambda item: str(item["closed_at"])):
        equity = float(row["equity_after"])
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _net_log_growth(rows: Iterable[Mapping[str, object]]) -> float:
    """Pool the realized, cost-inclusive equity factor of every closed trade.

    For the primary global ledger these terms telescope to
    ``log(final_equity / initial_equity)``.  The same calculation is retained
    for the window-reset panel, but that panel is diagnostic only.
    """

    terms: list[float] = []
    for row in rows:
        before = float(row["equity_before"])
        after = float(row["equity_after"])
        if not math.isfinite(before) or not math.isfinite(after):
            raise ValueError("equity ledger values must be finite")
        if before <= 0 or after <= 0:
            raise ValueError("log growth requires positive equity")
        terms.append(math.log(after / before))
    return math.fsum(terms)


def _metrics(
    rows: list[dict[str, object]],
    *,
    initial_equity: float,
    instrument_days: int,
    portfolio_operating_days: int,
    equity_scope: str = "global_portfolio",
    include_drawdown: bool = True,
) -> dict[str, object]:
    if instrument_days <= 0 or portfolio_operating_days <= 0:
        raise ValueError("performance denominators must be positive")
    pnls = [float(row["net_pnl"]) for row in rows]
    rs = [float(row["net_r"]) for row in rows]
    positive = [value for value in rs if value > 0]
    negative = [value for value in rs if value < 0]
    gross_win = math.fsum(value for value in pnls if value > 0)
    gross_loss = -math.fsum(value for value in pnls if value < 0)
    log_growth = _net_log_growth(rows)
    if equity_scope not in {"global_portfolio", "window_panel"}:
        raise ValueError("unknown equity scope")
    max_drawdown = (
        _global_max_drawdown_fraction(rows, initial_equity)
        if include_drawdown and equity_scope == "global_portfolio"
        else _panel_max_drawdown_fraction(rows, initial_equity)
        if include_drawdown
        else None
    )
    compounded_return = math.expm1(log_growth)
    return {
        "equity_scope": equity_scope,
        "trades": len(rows),
        "instrument_days": instrument_days,
        "trades_per_instrument_day": len(rows) / instrument_days,
        "portfolio_operating_days": portfolio_operating_days,
        "completed_trades_per_portfolio_operating_day": (
            len(rows) / portfolio_operating_days
        ),
        "wins": len(positive),
        "losses": len(negative),
        "win_rate": None if not rows else len(positive) / len(rows),
        "net_pnl": math.fsum(pnls),
        "net_r": math.fsum(rs),
        "net_r_per_instrument_day": math.fsum(rs) / instrument_days,
        "net_log_growth": log_growth,
        "net_log_growth_per_instrument_day": log_growth / instrument_days,
        "net_log_growth_per_portfolio_operating_day": (
            log_growth / portfolio_operating_days
        ),
        "cumulative_return": compounded_return,
        "cumulative_log_compounded_return": compounded_return,
        "average_net_r": None if not rows else statistics.fmean(rs),
        "profit_factor": None if gross_loss == 0 else gross_win / gross_loss,
        "median_stop_distance_pct": (
            None
            if not rows
            else statistics.median(
                float(row["stop_distance_pct"]) for row in rows
            )
        ),
        "median_target_r": (
            None
            if not rows
            else statistics.median(float(row["target_r"]) for row in rows)
        ),
        "partial_enabled": sum(bool(row["partial_enabled"]) for row in rows),
        "partial_1r_trades": sum(bool(row["partial_1r"]) for row in rows),
        "volume_exits": sum(
            row["final_reason"] == "volume_spike" for row in rows
        ),
        "max_drawdown_fraction": max_drawdown,
    }


def _diagnostic_values(diagnostics: object) -> dict[str, object]:
    if is_dataclass(diagnostics):
        return asdict(diagnostics)
    if isinstance(diagnostics, Mapping):
        return dict(diagnostics)
    return {}


def _authority_priority(
    opportunity: Opportunity,
    context: WindowContext,
) -> tuple[object, ...]:
    """Resolve only genuine same-cutoff competition for the shared slot."""

    authority = opportunity.authority
    return (
        0
        if opportunity.scene_family
        is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
        else 1,
        0 if bool(getattr(authority, "has_literal_body_overlap", False)) else 1,
        float(authority.zone.width),
        -authority.known_at.value,
        context.symbol,
        str(authority.authority_id),
    )


def _assemble_global_candidate(
    context: WindowContext,
    authority: object,
    *,
    costs: CostConfig,
    execution_arm: str,
    assemble_v07_opportunity: Callable[..., object],
) -> Opportunity | OpportunityRejection:
    if authority.scene_family is SceneFamily.SR_FLIP_FVG:
        return assemble_v07_opportunity(
            context.book,
            authority,
            costs=costs,
            entry_arm=execution_arm,
        )
    if isinstance(authority, ConfluenceAuthority) and authority.destination is None:
        return assemble_v03_opportunity(
            context.book,
            authority,
            as_of=authority.known_at,
            costs=SimpleExecutionCosts(
                entry_fee_rate=costs.entry_fee_rate,
                exit_fee_rate=costs.target_fee_rate,
            ),
            event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
        )
    return assemble_v04_opportunity(context.book, authority, costs=costs)


def _v07_next_open_is_executable(
    context: WindowContext,
    opportunity: Opportunity,
    *,
    costs: CostConfig,
) -> bool:
    frame = context.book.frames[Timeframe.M5]
    later = frame.loc[frame.index >= opportunity.known_at]
    if later.empty:
        return False
    actual_open = float(later.iloc[0]["open"])
    valid_geometry = (
        opportunity.initial_stop < actual_open < opportunity.target.order_price
        if opportunity.side.value == "long"
        else opportunity.target.order_price
        < actual_open
        < opportunity.initial_stop
    )
    if not valid_geometry:
        return False
    return (
        select_initial_target(
            (opportunity.target,),
            side=opportunity.side,
            entry_price=actual_open,
            tick_size=context.book.tick_size,
            costs=SimpleExecutionCosts(
                entry_fee_rate=costs.entry_fee_rate,
                exit_fee_rate=costs.target_fee_rate,
            ),
        ).target
        is not None
    )


def _run_global_arm(
    contexts: Sequence[WindowContext],
    *,
    arm: str,
    authority_scope: str,
    execution_arm: str,
    initial_equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    assemble_v07_opportunity: Callable[..., object],
) -> GlobalReplayResult:
    """Chronologically replay all symbols with one equity and one occupied slot."""

    candidates_by_cutoff: dict[
        pd.Timestamp, list[tuple[WindowContext, object]]
    ] = defaultdict(list)
    for context in contexts:
        authorities = (
            context.leader
            if authority_scope == "leader"
            else context.v07
            if authority_scope == "v07"
            else (*context.leader, *context.v07)
            if authority_scope == "combined"
            else None
        )
        if authorities is None:
            raise ValueError("unknown global authority scope")
        for authority in authorities:
            candidates_by_cutoff[authority.known_at].append((context, authority))

    current_equity = float(initial_equity)
    occupied_until: pd.Timestamp | None = None
    closed_attempts: list[GlobalClosedAttempt] = []
    opportunity_rejections = 0
    sizing_rejections = 0
    pending_cancellations = 0
    entry_rejections = 0
    open_censored = 0
    entry_censored = 0
    slot_suppressed = 0
    simultaneous_candidate_cutoffs = 0
    simultaneous_candidates = 0

    for cutoff in sorted(candidates_by_cutoff):
        raw = candidates_by_cutoff[cutoff]
        if occupied_until is not None and cutoff < occupied_until:
            slot_suppressed += len(raw)
            continue

        opportunities: list[tuple[Opportunity, WindowContext, object]] = []
        for context, authority in raw:
            result = _assemble_global_candidate(
                context,
                authority,
                costs=costs,
                execution_arm=execution_arm,
                assemble_v07_opportunity=assemble_v07_opportunity,
            )
            if isinstance(result, OpportunityRejection):
                opportunity_rejections += 1
                continue
            opportunities.append((result, context, authority))
        if len(opportunities) > 1:
            simultaneous_candidate_cutoffs += 1
            simultaneous_candidates += len(opportunities)
        if not opportunities:
            continue
        opportunities.sort(key=lambda item: _authority_priority(item[0], item[1]))

        selected: tuple[Opportunity, WindowContext, object] | None = None
        intent = None
        for opportunity, context, authority in opportunities:
            selected_entry_mode = (
                EntryMode.NEXT_BAR_OPEN
                if authority.scene_family is SceneFamily.SR_FLIP_FVG
                and execution_arm == BOUNDARY_ACCEPT_NEXT_OPEN
                else EntryMode.LIMIT_FIRST_REVISIT
            )
            if (
                authority.scene_family is SceneFamily.SR_FLIP_FVG
                and selected_entry_mode is EntryMode.NEXT_BAR_OPEN
                and not _v07_next_open_is_executable(
                    context,
                    opportunity,
                    costs=costs,
                )
            ):
                opportunity_rejections += 1
                continue
            try:
                intent = intent_from_opportunity(
                    opportunity,
                    equity=current_equity,
                    costs=costs,
                    risk=risk,
                    event_created_entry_mode=selected_entry_mode,
                )
            except ValueError:
                sizing_rejections += 1
                continue
            selected = (opportunity, context, authority)
            break
        if selected is None or intent is None:
            continue

        opportunity, context, authority = selected
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
        expiration = _preentry_expiration(context.book, opportunity, replay)
        if expiration is not None:
            pending_cancellations += 1
            occupied_until = expiration[0]
            continue
        if replay.status == "ENTRY_REJECTED":
            entry_rejections += 1
            occupied_until = replay.events[-1].occurred_at
            continue
        if replay.trade is not None:
            current_equity += replay.trade.net_pnl
            attempt = SimpleNamespace(
                result=replay,
                intent=intent,
                authority_id=authority.authority_id,
                equity_before=equity_before,
                equity_after=current_equity,
            )
            closed_attempts.append(
                GlobalClosedAttempt(
                    context=context,
                    authority=authority,
                    attempt=attempt,
                )
            )
            occupied_until = replay.trade.closed_at
            continue
        if replay.status == "OPEN_CENSORED":
            open_censored += 1
        else:
            entry_censored += 1
        # Representative windows have deliberate gaps. The unresolved order
        # owns the shared slot through its supplied context only.
        occupied_until = context.end

    return GlobalReplayResult(
        closed_attempts=tuple(closed_attempts),
        initial_equity=float(initial_equity),
        final_equity=current_equity,
        opportunity_rejections=opportunity_rejections,
        sizing_rejections=sizing_rejections,
        pending_cancellations=pending_cancellations,
        entry_rejections=entry_rejections,
        open_censored=open_censored,
        entry_censored=entry_censored,
        slot_suppressed_authorities=slot_suppressed,
        simultaneous_candidate_cutoffs=simultaneous_candidate_cutoffs,
        simultaneous_candidates=simultaneous_candidates,
    )


def main() -> int:
    args = _args()
    indices = (
        (args.window_index,)
        if args.window_index is not None
        else tuple(range(len(WINDOWS)))
    )
    if any(index < 0 or index >= len(WINDOWS) for index in indices):
        raise SystemExit("window-index out of range")

    selected_windows = tuple(WINDOWS[index] for index in indices)
    instrument_days = _instrument_days(selected_windows)
    portfolio_dates = _portfolio_operating_dates(selected_windows)
    portfolio_days = len(portfolio_dates)
    (
        build_v07_result,
        assemble_v07_opportunity,
        run_v07_historical_replay,
    ) = _load_v07_api()
    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    frames = {
        symbol: _load_source(args.data_dir / f"{symbol}_5m.csv")
        for symbol in {WINDOWS[index][0] for index in indices}
    }
    ledger: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    contexts: list[WindowContext] = []

    for ordinal, index in enumerate(indices, start=1):
        symbol, environment, start, end, tick_size = WINDOWS[index]
        candles = _window(frames[symbol], start, end)
        print(
            f"[{ordinal}/{len(indices)}] {symbol} {environment}: "
            f"{len(candles)} bars",
            flush=True,
        )
        book = build_feature_book(candles, symbol=symbol, tick_size=tick_size)
        baseline_break_retest = tuple(
            authority
            for authority in build_baseline_event_authorities(book)
            if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
        )
        v05 = build_m15_m5_liquidity_delivery_result(book)
        leader = tuple(
            sorted(
                (*baseline_break_retest, *v05.authorities),
                key=lambda item: (item.known_at, item.authority_id),
            )
        )
        v07_result = build_v07_result(book)
        v07_authorities = tuple(getattr(v07_result, "authorities"))
        combined = tuple(
            sorted(
                (*leader, *v07_authorities),
                key=lambda item: (item.known_at, item.authority_id),
            )
        )
        context = WindowContext(
            index=index,
            symbol=symbol,
            environment=environment,
            start=_utc(start),
            end=_utc(end),
            tick_size=float(tick_size),
            candles=candles,
            book=book,
            leader=leader,
            v07=v07_authorities,
        )
        contexts.append(context)
        arm_inputs = (
            (ARMS[0], leader, "leader_locked_limit", "leader", None),
            (
                ARMS[1],
                v07_authorities,
                FIRST_RETURN_LIMIT,
                "v07",
                False,
            ),
            (
                ARMS[2],
                v07_authorities,
                BOUNDARY_ACCEPT_NEXT_OPEN,
                "v07",
                False,
            ),
            (
                ARMS[3],
                combined,
                FIRST_RETURN_LIMIT,
                "v07",
                None,
            ),
            (
                ARMS[4],
                combined,
                BOUNDARY_ACCEPT_NEXT_OPEN,
                "v07",
                None,
            ),
        )
        for arm, authorities, entry_arm, runner_kind, dynamic_targets in arm_inputs:
            if runner_kind == "leader":
                run = run_v04_historical_replay(
                    candles,
                    symbol=symbol,
                    tick_size=tick_size,
                    equity=args.initial_equity,
                    costs=COSTS,
                    risk=risk,
                    book=book,
                    authorities=authorities,
                    use_v03_targets=None,
                )
            else:
                run = run_v07_historical_replay(
                    candles,
                    symbol=symbol,
                    tick_size=tick_size,
                    equity=args.initial_equity,
                    costs=COSTS,
                    risk=risk,
                    book=book,
                    authorities=authorities,
                    entry_arm=entry_arm,
                    use_v03_targets=dynamic_targets,
                )
            authority_map = {
                authority.authority_id: authority for authority in run.authorities
            }
            for attempt in run.attempts:
                if attempt.result.trade is None:
                    continue
                ledger.append(
                    _trade_row(
                        arm=arm,
                        scope="window_panel",
                        entry_arm=entry_arm,
                        window_index=index,
                        symbol=symbol,
                        environment=environment,
                        candles=candles,
                        attempt=attempt,
                        authority=authority_map[attempt.authority_id],
                    )
                )
            row: dict[str, object] = {
                "arm": arm,
                "scope": "window_panel",
                "entry_arm": entry_arm,
                "window_index": index,
                "symbol": symbol,
                "environment": environment,
                "authorities": len(run.authorities),
                "attempts": len(run.attempts),
                "closed_trades": len(run.closed_trades),
                "opportunity_rejections": len(run.opportunity_rejections),
                "sizing_rejections": len(run.sizing_rejections),
                "pending_cancellations": len(run.pending_cancellations),
                "slot_suppressed_authorities": run.slot_suppressed_authorities,
                "cancellation_reasons": json.dumps(
                    Counter(item.reason for item in run.pending_cancellations),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "final_equity": run.final_equity,
            }
            if runner_kind == "v07":
                row.update(
                    {
                        f"funnel_{key}": value
                        for key, value in _diagnostic_values(
                            getattr(v07_result, "diagnostics")
                        ).items()
                    }
                )
            diagnostics.append(row)
            print(
                f"  {arm}: authorities={len(run.authorities)}, "
                f"closed={len(run.closed_trades)}, "
                f"equity={run.final_equity:.2f}",
                flush=True,
            )

    global_results: dict[str, GlobalReplayResult] = {}
    global_specs = (
        (ARMS[0], "leader", "leader_locked_limit"),
        (ARMS[1], "v07", FIRST_RETURN_LIMIT),
        (ARMS[2], "v07", BOUNDARY_ACCEPT_NEXT_OPEN),
        (ARMS[3], "combined", FIRST_RETURN_LIMIT),
        (ARMS[4], "combined", BOUNDARY_ACCEPT_NEXT_OPEN),
    )
    for arm, authority_scope, execution_arm in global_specs:
        result = _run_global_arm(
            contexts,
            arm=arm,
            authority_scope=authority_scope,
            execution_arm=execution_arm,
            initial_equity=args.initial_equity,
            costs=COSTS,
            risk=risk,
            assemble_v07_opportunity=assemble_v07_opportunity,
        )
        global_results[arm] = result
        for closed in result.closed_attempts:
            ledger.append(
                _trade_row(
                    arm=arm,
                    scope="global_portfolio",
                    entry_arm=execution_arm,
                    window_index=closed.context.index,
                    symbol=closed.context.symbol,
                    environment=closed.context.environment,
                    candles=closed.context.candles,
                    attempt=closed.attempt,
                    authority=closed.authority,
                )
            )
        diagnostics.append(
            {
                "arm": arm,
                "scope": "global_portfolio",
                "entry_arm": execution_arm,
                "window_index": None,
                "symbol": "GLOBAL",
                "environment": "all_sampled_windows",
                "authorities": sum(
                    len(context.leader)
                    if authority_scope == "leader"
                    else len(context.v07)
                    if authority_scope == "v07"
                    else len(context.leader) + len(context.v07)
                    for context in contexts
                ),
                "attempts": len(result.closed_attempts)
                + result.entry_rejections
                + result.open_censored
                + result.entry_censored,
                "closed_trades": len(result.closed_attempts),
                "opportunity_rejections": result.opportunity_rejections,
                "sizing_rejections": result.sizing_rejections,
                "pending_cancellations": result.pending_cancellations,
                "slot_suppressed_authorities": (
                    result.slot_suppressed_authorities
                ),
                "simultaneous_candidate_cutoffs": (
                    result.simultaneous_candidate_cutoffs
                ),
                "simultaneous_candidates": result.simultaneous_candidates,
                "entry_rejections": result.entry_rejections,
                "open_censored": result.open_censored,
                "entry_censored": result.entry_censored,
                "final_equity": result.final_equity,
            }
        )
        print(
            f"  GLOBAL {arm}: closed={len(result.closed_attempts)}, "
            f"equity={result.final_equity:.2f}",
            flush=True,
        )

    def metrics(
        selected: list[dict[str, object]],
        *,
        days_by_instrument: int = instrument_days,
        days_by_portfolio: int = portfolio_days,
        equity_scope: str = "global_portfolio",
        include_drawdown: bool = True,
    ) -> dict[str, object]:
        return _metrics(
            selected,
            initial_equity=args.initial_equity,
            instrument_days=days_by_instrument,
            portfolio_operating_days=days_by_portfolio,
            equity_scope=equity_scope,
            include_drawdown=include_drawdown,
        )

    portfolio_metrics = {
        arm: metrics(
            [
                row
                for row in ledger
                if row["arm"] == arm
                and row["scope"] == "global_portfolio"
            ]
        )
        for arm in ARMS
    }
    for arm, result in global_results.items():
        portfolio_metrics[arm].update(
            {
                "initial_equity": result.initial_equity,
                "final_equity": result.final_equity,
                "net_return": result.final_equity / result.initial_equity - 1,
            }
        )
        ledger_log = float(portfolio_metrics[arm]["net_log_growth"])
        terminal_log = math.log(result.final_equity / result.initial_equity)
        if not math.isclose(ledger_log, terminal_log, abs_tol=1e-12):
            raise RuntimeError("global trade ledger and final equity disagree")

    panel_metrics = {
        arm: metrics(
            [
                row
                for row in ledger
                if row["arm"] == arm and row["scope"] == "window_panel"
            ],
            equity_scope="window_panel",
        )
        for arm in ARMS
    }
    leader_metrics = portfolio_metrics[ARMS[0]]
    summary = {
        "contract": {
            "windows": len(indices),
            "window_indices": list(indices),
            "instrument_days": instrument_days,
            "portfolio_operating_days": portfolio_days,
            "portfolio_day_definition": (
                "unique UTC calendar dates touched by selected half-open "
                "window intervals; overlapping symbols count once"
            ),
            "log_growth_definition": (
                "sum of ln(equity_after/equity_before) over completed trade "
                "rows from the chronological shared-equity ledger, divided "
                "by unique portfolio operating dates"
            ),
            "equity_ledger_basis": (
                "one chronological shared equity and shared occupied_until "
                "across all selected BTC/ETH contexts; fees and slippage included"
            ),
            "portfolio_scope_note": (
                "window-reset ledgers remain only as panel diagnostics and do "
                "not supply the primary portfolio metrics"
            ),
            "subset_drawdown_note": (
                "environment and source-strategy subsets are not standalone "
                "equity paths, so their max_drawdown_fraction is null"
            ),
            "initial_portfolio_equity": args.initial_equity,
            "window_panel_initial_equity_per_window": args.initial_equity,
            "risk_fraction": args.risk_fraction,
            "daily_loss_limit_enabled": False,
            "one_total_slot_global_portfolio": True,
            "v07_entry_arms_are_separate_runs": True,
            "partial_policy": (
                "target_R>=1.4: half_at_1R_then_BE; else full_target"
            ),
            "volume_exit": (
                "M5_or_M15_RVOL20_median>=2_and_net_profitable"
            ),
            "costs": {
                "entry_fee_rate": COSTS.entry_fee_rate,
                "stop_fee_rate": COSTS.stop_fee_rate,
                "target_fee_rate": COSTS.target_fee_rate,
                "volume_exit_fee_rate": COSTS.volume_exit_fee_rate,
                "stop_slippage_bps": COSTS.stop_slippage_bps,
                "volume_exit_slippage_bps": COSTS.volume_exit_slippage_bps,
            },
        },
        "arms": portfolio_metrics,
        "window_panel_arms": panel_metrics,
        "leader_delta": {
            arm: {
                "trade_delta": portfolio_metrics[arm]["trades"]
                - leader_metrics["trades"],
                "net_r_delta": portfolio_metrics[arm]["net_r"]
                - leader_metrics["net_r"],
                "net_log_growth_delta": portfolio_metrics[arm]["net_log_growth"]
                - leader_metrics["net_log_growth"],
            }
            for arm in ARMS[1:]
        },
        "portfolio_by_environment": {
            arm: {
                environment: metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["scope"] == "global_portfolio"
                        and row["environment"] == environment
                    ],
                    days_by_instrument=len(
                        _dates_in_half_open_window(start, end)
                    ),
                    days_by_portfolio=len(
                        _dates_in_half_open_window(start, end)
                    ),
                    include_drawdown=False,
                )
                for _, environment, start, end, _ in selected_windows
            }
            for arm in ARMS
        },
        "by_source_strategy": {
            arm: {
                source: metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["scope"] == "global_portfolio"
                        and row["source_strategy"] == source
                    ],
                    include_drawdown=False,
                )
                for source in ("leader", "v07")
            }
            for arm in ARMS
        },
        "window_panel_by_environment": {
            arm: {
                environment: metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["scope"] == "window_panel"
                        and row["environment"] == environment
                    ],
                    days_by_instrument=len(
                        _dates_in_half_open_window(start, end)
                    ),
                    days_by_portfolio=len(
                        _dates_in_half_open_window(start, end)
                    ),
                    equity_scope="window_panel",
                )
                for _, environment, start, end, _ in selected_windows
            }
            for arm in ARMS
        },
        "diagnostics": diagnostics,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    pd.DataFrame.from_records(ledger).to_csv(
        args.output_dir / "trade_ledger.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame.from_records(diagnostics).to_csv(
        args.output_dir / "diagnostics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(json.dumps(summary["arms"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
