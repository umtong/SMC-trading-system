from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from .domain import EntryMode, OBCausalState, Side, Timeframe
from .execution import (
    CostConfig,
    OrderIntent,
    RiskConfig,
    TradeRecord,
    build_confluence_intent,
)
from .features import TIMEFRAME_DELTA, validate_ohlcv
from .pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    OpportunityResult,
    StructureSnapshot,
    assemble_confluence_opportunities,
    build_feature_book,
    structure_snapshot,
)
from .replay import ReplayResult, replay_intent
from .strategy import SimpleExecutionCosts


@dataclass(frozen=True, slots=True)
class SizingRejection:
    opportunity_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class SnapshotPlan:
    as_of: pd.Timestamp
    book: FeatureBook
    structure: StructureSnapshot
    results: tuple[OpportunityResult, ...]
    candidate_intents: tuple[OrderIntent, ...]
    sizing_rejections: tuple[SizingRejection, ...]
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN

    @property
    def opportunities(self) -> tuple[Opportunity, ...]:
        return tuple(item for item in self.results if isinstance(item, Opportunity))

    @property
    def opportunity_rejections(self) -> tuple[OpportunityRejection, ...]:
        return tuple(
            item for item in self.results if isinstance(item, OpportunityRejection)
        )


@dataclass(frozen=True, slots=True)
class ReplayAttempt:
    opportunity_id: str
    authority_id: str
    intent: OrderIntent
    result: ReplayResult
    equity_before: float
    equity_after: float


@dataclass(frozen=True, slots=True)
class PendingCancellation:
    opportunity_id: str
    authority_id: str
    order_id: str
    cancelled_at: pd.Timestamp
    reason: str


@dataclass(frozen=True, slots=True)
class OpportunityExpiration:
    opportunity_id: str
    authority_id: str
    expired_at: pd.Timestamp
    reason: str


@dataclass(frozen=True, slots=True)
class DailyLossStatus:
    local_date: date
    day_start_equity: float
    realized_net_pnl: float
    loss_limit_cash: float
    remaining_loss_budget: float
    blocked: bool


@dataclass(frozen=True, slots=True)
class DailyLossBlock:
    decision_at: pd.Timestamp
    local_date: date
    day_start_equity: float
    realized_net_pnl: float
    loss_limit_cash: float


class DailyLossGuard:
    """Track realized daily net PnL and cap only newly proposed order risk."""

    def __init__(self, risk: RiskConfig) -> None:
        self.risk = risk
        self._timezone = ZoneInfo(risk.daily_reset_timezone)
        self._day_start_equity: dict[date, float] = {}
        self._realized_net_pnl: dict[date, float] = {}

    def _local_date(self, value: object) -> date:
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp.tz is None:
            raise ValueError("daily loss timestamps must be timezone-aware")
        return timestamp.tz_convert(self._timezone).date()

    def status(self, *, at: object, equity: float) -> DailyLossStatus:
        account_equity = float(equity)
        if account_equity <= 0:
            raise ValueError("equity must be positive for daily loss control")
        local_date = self._local_date(at)
        day_start = self._day_start_equity.setdefault(local_date, account_equity)
        realized = self._realized_net_pnl.get(local_date, 0.0)
        limit_cash = day_start * self.risk.daily_loss_limit_fraction
        remaining = max(0.0, limit_cash + realized)
        return DailyLossStatus(
            local_date=local_date,
            day_start_equity=day_start,
            realized_net_pnl=realized,
            loss_limit_cash=limit_cash,
            remaining_loss_budget=remaining,
            blocked=self.risk.daily_loss_limit_enabled and remaining <= 1e-12,
        )

    def risk_for_new_order(self, *, at: object, equity: float) -> RiskConfig:
        if not self.risk.daily_loss_limit_enabled:
            return self.risk
        status = self.status(at=at, equity=equity)
        if status.blocked:
            raise ValueError("daily realized loss limit has been reached")
        normal_budget = float(equity) * self.risk.risk_fraction
        effective_budget = min(normal_budget, status.remaining_loss_budget)
        return replace(self.risk, risk_fraction=effective_budget / float(equity))

    def record_realized(
        self,
        *,
        closed_at: object,
        net_pnl: float,
        equity_before: float,
    ) -> None:
        if not self.risk.daily_loss_limit_enabled:
            return
        local_date = self._local_date(closed_at)
        self._day_start_equity.setdefault(local_date, float(equity_before))
        self._realized_net_pnl[local_date] = (
            self._realized_net_pnl.get(local_date, 0.0) + float(net_pnl)
        )


@dataclass(frozen=True, slots=True)
class HistoricalReplayRun:
    book: FeatureBook
    decision_times: tuple[pd.Timestamp, ...]
    attempts: tuple[ReplayAttempt, ...]
    opportunity_rejections: tuple[OpportunityRejection, ...]
    sizing_rejections: tuple[SizingRejection, ...]
    initial_equity: float
    final_equity: float
    pending_cancellations: tuple[PendingCancellation, ...] = ()
    expired_before_submission: tuple[OpportunityExpiration, ...] = ()
    daily_loss_blocks: tuple[DailyLossBlock, ...] = ()
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN

    @property
    def closed_trades(self) -> tuple[TradeRecord, ...]:
        return tuple(
            attempt.result.trade
            for attempt in self.attempts
            if attempt.result.trade is not None
        )


def load_5m_csv(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    frame = pd.read_csv(source)
    timestamp_column = next(
        (
            name
            for name in ("open_time", "timestamp", "time", "datetime")
            if name in frame.columns
        ),
        None,
    )
    if timestamp_column is None:
        raise ValueError(
            "CSV requires one timestamp column: open_time, timestamp, time, or datetime"
        )
    timestamps = pd.to_datetime(frame.pop(timestamp_column), utc=True, errors="raise")
    frame.index = pd.DatetimeIndex(timestamps, name="open_time")
    return validate_ohlcv(frame, expected_timeframe=Timeframe.M5)


def _target_selection_costs(costs: CostConfig) -> SimpleExecutionCosts:
    return SimpleExecutionCosts(
        entry_fee_rate=costs.entry_fee_rate,
        exit_fee_rate=costs.target_fee_rate,
    )


def intent_from_opportunity(
    opportunity: Opportunity,
    *,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> OrderIntent:
    entry_mode = (
        EntryMode(event_created_entry_mode)
        if opportunity.planned_entry.ob_causal_state is OBCausalState.EVENT_CREATED
        else EntryMode.LIMIT_FIRST_REVISIT
    )
    return build_confluence_intent(
        order_id=f"order:{opportunity.opportunity_id}",
        source_id=opportunity.authority_id,
        symbol=opportunity.symbol,
        side=opportunity.side,
        created_at=opportunity.known_at,
        initial_stop=opportunity.initial_stop,
        initial_target=opportunity.target.order_price,
        equity=equity,
        costs=costs,
        risk=risk,
        entry_reference=opportunity.planned_entry.price,
        entry_mode=entry_mode,
        ob_causal_state=opportunity.planned_entry.ob_causal_state,
        scene_family=opportunity.scene_family,
    )


def _plan_from_book(
    book: FeatureBook,
    *,
    cutoff: pd.Timestamp,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    excluded_authority_ids: frozenset[str] = frozenset(),
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> SnapshotPlan:
    results = assemble_confluence_opportunities(
        book,
        as_of=cutoff,
        costs=_target_selection_costs(costs),
        excluded_authority_ids=excluded_authority_ids,
        event_created_entry_mode=event_created_entry_mode,
    )
    intents: list[OrderIntent] = []
    sizing_rejections: list[SizingRejection] = []
    for result in results:
        if not isinstance(result, Opportunity):
            continue
        try:
            intents.append(
                intent_from_opportunity(
                    result,
                    equity=equity,
                    costs=costs,
                    risk=risk,
                    event_created_entry_mode=event_created_entry_mode,
                )
            )
        except ValueError as exc:
            sizing_rejections.append(SizingRejection(result.opportunity_id, str(exc)))
    if len(intents) > 1:
        raise RuntimeError("the V0 single-slot planner produced multiple orders")
    return SnapshotPlan(
        as_of=cutoff,
        book=book,
        structure=structure_snapshot(book, as_of=cutoff),
        results=results,
        candidate_intents=tuple(intents),
        sizing_rejections=tuple(sizing_rejections),
        event_created_entry_mode=EntryMode(event_created_entry_mode),
    )


def plan_snapshot(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    tick_size: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    as_of: object | None = None,
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> SnapshotPlan:
    validated = validate_ohlcv(candles_5m, expected_timeframe=Timeframe.M5)
    if validated.empty:
        raise ValueError("at least one completed 5m candle is required")
    cutoff = (
        validated.index[-1] + pd.Timedelta(minutes=5)
        if as_of is None
        else pd.Timestamp(as_of)
    )
    if cutoff.tz is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = cutoff.tz_convert("UTC")
    book = build_feature_book(validated, symbol=symbol, tick_size=tick_size)
    return _plan_from_book(
        book,
        cutoff=cutoff,
        equity=equity,
        costs=costs,
        risk=risk,
        event_created_entry_mode=event_created_entry_mode,
    )


def _first_target_exhaustion(
    book: FeatureBook, opportunity: Opportunity
) -> pd.Timestamp | None:
    authority = opportunity.authority
    frame = book.frames[Timeframe.M5]
    later = frame.loc[frame.index >= authority.known_at]
    touched = later.loc[
        later["high"] >= opportunity.target.order_price
        if authority.side is Side.LONG
        else later["low"] <= opportunity.target.order_price
    ]
    if touched.empty:
        return None
    return touched.index[0] + TIMEFRAME_DELTA[Timeframe.M5]


def _first_event_invalidation(
    book: FeatureBook, opportunity: Opportunity
) -> pd.Timestamp | None:
    authority = opportunity.authority
    event = next(
        item
        for item in book.liquidity_events[authority.confirmation.liquidity_event_timeframe]
        if item.event_id == authority.confirmation.liquidity_event_id
    )
    frame = book.frames[event.timeframe]
    closes = frame.index + TIMEFRAME_DELTA[event.timeframe]
    later = frame.loc[closes > authority.known_at]
    later_closes = later.index + TIMEFRAME_DELTA[event.timeframe]
    invalid = (
        later["close"] <= event.node_price - book.tick_size + 1e-12
        if authority.side is Side.LONG
        else later["close"] >= event.node_price + book.tick_size - 1e-12
    )
    matches = later_closes[invalid.to_numpy()]
    return None if len(matches) == 0 else matches[0]


def _first_opportunity_expiration(
    book: FeatureBook, opportunity: Opportunity
) -> tuple[pd.Timestamp, str] | None:
    """Return the first structural expiry of an unfilled opportunity."""

    choices = tuple(
        item
        for item in (
            (
                _first_target_exhaustion(book, opportunity),
                "initial_target_used_before_entry",
            ),
            (
                _first_event_invalidation(book, opportunity),
                "liquidity_event_invalidated_before_entry",
            ),
        )
        if item[0] is not None
    )
    return None if not choices else min(choices, key=lambda item: (item[0], item[1]))


def _entry_fill_time(replay: ReplayResult) -> pd.Timestamp | None:
    if replay.trade is not None:
        return replay.trade.entry_time
    if replay.open_position is not None:
        return replay.open_position.filled_at
    return None


def _pending_cancellation(
    book: FeatureBook,
    opportunity: Opportunity,
    replay: ReplayResult,
) -> tuple[pd.Timestamp, str] | None:
    expiration = _first_opportunity_expiration(book, opportunity)
    if expiration is None:
        return None
    cancelled_at, reason = expiration
    fill_time = _entry_fill_time(replay)
    # A completed-bar cancellation cannot pre-empt an entry that occurred
    # somewhere inside that same smallest replay bar.
    if fill_time is not None and cancelled_at >= fill_time:
        return None
    if replay.status == "ENTRY_REJECTED":
        rejected_at = next(
            (
                event.occurred_at
                for event in replay.events
                if event.kind == "entry_rejected"
            ),
            replay.events[-1].occurred_at,
        )
        if rejected_at <= cancelled_at:
            return None
    return cancelled_at, reason


def run_historical_replay(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    tick_size: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> HistoricalReplayRun:
    """Run V0 sequentially on OB completion clocks with one account slot."""

    validated = validate_ohlcv(candles_5m, expected_timeframe=Timeframe.M5)
    if validated.empty:
        raise ValueError("at least one completed 5m candle is required")
    book = build_feature_book(validated, symbol=symbol, tick_size=tick_size)
    decision_times = tuple(
        sorted(
            {
                block.known_at
                for timeframe in (Timeframe.M5, Timeframe.M15)
                for block in book.order_blocks[timeframe]
            }
        )
    )
    current_equity = float(equity)
    attempts: list[ReplayAttempt] = []
    opportunity_rejections: list[OpportunityRejection] = []
    sizing_rejections: list[SizingRejection] = []
    pending_cancellations: list[PendingCancellation] = []
    expired_before_submission: list[OpportunityExpiration] = []
    daily_loss_blocks: list[DailyLossBlock] = []
    recorded_block_days: set[date] = set()
    submitted_authorities: set[str] = set()
    occupied_until: pd.Timestamp | None = None
    daily_loss_guard = DailyLossGuard(risk)

    for cutoff in decision_times:
        if occupied_until is not None and cutoff < occupied_until:
            continue
        daily_status = daily_loss_guard.status(at=cutoff, equity=current_equity)
        if daily_status.blocked:
            if daily_status.local_date not in recorded_block_days:
                daily_loss_blocks.append(
                    DailyLossBlock(
                        decision_at=cutoff,
                        local_date=daily_status.local_date,
                        day_start_equity=daily_status.day_start_equity,
                        realized_net_pnl=daily_status.realized_net_pnl,
                        loss_limit_cash=daily_status.loss_limit_cash,
                    )
                )
                recorded_block_days.add(daily_status.local_date)
            continue
        effective_risk = daily_loss_guard.risk_for_new_order(
            at=cutoff,
            equity=current_equity,
        )
        plan = _plan_from_book(
            book,
            cutoff=cutoff,
            equity=current_equity,
            costs=costs,
            risk=effective_risk,
            excluded_authority_ids=frozenset(submitted_authorities),
            event_created_entry_mode=event_created_entry_mode,
        )
        if not plan.results:
            continue
        result = plan.results[0]
        if result.authority_id in submitted_authorities:
            continue
        submitted_authorities.add(result.authority_id)

        if isinstance(result, OpportunityRejection):
            opportunity_rejections.append(result)
            continue
        expiration = _first_opportunity_expiration(book, result)
        if expiration is not None and expiration[0] <= cutoff:
            expired_before_submission.append(
                OpportunityExpiration(
                    opportunity_id=result.opportunity_id,
                    authority_id=result.authority_id,
                    expired_at=expiration[0],
                    reason=expiration[1],
                )
            )
            continue
        if plan.sizing_rejections:
            sizing_rejections.extend(plan.sizing_rejections)
            continue
        if not plan.candidate_intents:
            continue

        intent = plan.candidate_intents[0]
        equity_before = current_equity
        replay = replay_intent(
            intent,
            candles=validated,
            candle_interval="5min",
            costs=costs,
            volume_bars={
                Timeframe.M5: validated,
                Timeframe.M15: book.frames[Timeframe.M15],
            },
        )
        cancellation = _pending_cancellation(book, result, replay)
        if cancellation is not None:
            cancelled_at, reason = cancellation
            pending_cancellations.append(
                PendingCancellation(
                    opportunity_id=result.opportunity_id,
                    authority_id=result.authority_id,
                    order_id=intent.order_id,
                    cancelled_at=cancelled_at,
                    reason=reason,
                )
            )
            occupied_until = cancelled_at
            continue
        if replay.trade is not None:
            current_equity += replay.trade.net_pnl
            daily_loss_guard.record_realized(
                closed_at=replay.trade.closed_at,
                net_pnl=replay.trade.net_pnl,
                equity_before=equity_before,
            )
        attempts.append(
            ReplayAttempt(
                opportunity_id=result.opportunity_id,
                authority_id=result.authority_id,
                intent=intent,
                result=replay,
                equity_before=equity_before,
                equity_after=current_equity,
            )
        )
        if replay.status == "CLOSED":
            assert replay.trade is not None
            occupied_until = replay.trade.closed_at
            continue
        if replay.status == "ENTRY_REJECTED":
            occupied_until = replay.events[-1].occurred_at
            continue
        # No-TTL policy: an unfilled order or open position with no structural
        # cancellation owns the only slot until the supplied history ends.
        break

    return HistoricalReplayRun(
        book=book,
        decision_times=decision_times,
        attempts=tuple(attempts),
        opportunity_rejections=tuple(opportunity_rejections),
        sizing_rejections=tuple(sizing_rejections),
        initial_equity=float(equity),
        final_equity=current_equity,
        pending_cancellations=tuple(pending_cancellations),
        expired_before_submission=tuple(expired_before_submission),
        daily_loss_blocks=tuple(daily_loss_blocks),
        event_created_entry_mode=EntryMode(event_created_entry_mode),
    )


__all__: Sequence[str] = (
    "HistoricalReplayRun",
    "DailyLossBlock",
    "DailyLossGuard",
    "DailyLossStatus",
    "PendingCancellation",
    "OpportunityExpiration",
    "ReplayAttempt",
    "SizingRejection",
    "SnapshotPlan",
    "intent_from_opportunity",
    "load_5m_csv",
    "plan_snapshot",
    "run_historical_replay",
    "_first_opportunity_expiration",
)



