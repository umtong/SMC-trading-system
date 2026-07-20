from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Sequence

import pandas as pd

from .application import ReplayAttempt, intent_from_opportunity
from .domain import EntryMode, Timeframe
from .execution import CostConfig, OrderIntent, RiskConfig
from .execution_economics import cost_inclusive_target_r
from .pipeline import Opportunity, OpportunityRejection
from .portfolio import (
    CandidateAssembler,
    ClosedPortfolioAttempt,
    PortfolioContext,
    _candidate_cutoffs,
    _entry_fill_time,
    _first_expiration,
)
from .replay import ReplayResult, replay_intent


OpenSlotCandidatePriority = Callable[
    [Opportunity, PortfolioContext, CostConfig], tuple[object, ...]
]


@dataclass(frozen=True, slots=True)
class OpenSlotPortfolioReplayResult:
    """One-equity portfolio with many pending orders but at most one open trade."""

    contexts: tuple[PortfolioContext, ...]
    closed_attempts: tuple[ClosedPortfolioAttempt, ...]
    initial_equity: float
    final_equity: float
    opportunity_rejections: int
    sizing_rejections: int
    exposure_rejections: int
    pending_cancellations: int
    entry_rejections: int
    open_censored: int
    entry_censored: int
    slot_suppressed_authorities: int
    simultaneous_candidate_cutoffs: int
    simultaneous_candidates: int
    pending_orders_created: int
    cross_cancelled_pending: int
    maximum_concurrent_pending: int

    @property
    def valid(self) -> bool:
        return self.open_censored == 0 and self.entry_censored == 0

    @property
    def trades(self) -> int:
        return len(self.closed_attempts)

    @property
    def wins(self) -> int:
        return sum(
            closed.attempt.result.trade is not None
            and closed.attempt.result.trade.net_pnl > 0
            for closed in self.closed_attempts
        )

    @property
    def net_r(self) -> float:
        return math.fsum(
            closed.attempt.result.trade.net_pnl / closed.attempt.intent.risk_budget
            for closed in self.closed_attempts
            if closed.attempt.result.trade is not None
        )

    @property
    def average_net_r(self) -> float | None:
        return None if self.trades == 0 else self.net_r / self.trades

    @property
    def equity_multiple(self) -> float:
        return self.final_equity / self.initial_equity

    @property
    def operating_days(self) -> int:
        dates: set[pd.Timestamp] = set()
        for context in self.contexts:
            dates.update(context.operating_dates)
        return len(dates)

    @property
    def trades_per_operating_day(self) -> float:
        return self.trades / self.operating_days

    @property
    def max_drawdown_fraction(self) -> float:
        peak = self.initial_equity
        worst = 0.0
        for closed in sorted(
            self.closed_attempts,
            key=lambda item: item.attempt.result.trade.closed_at,
        ):
            equity = closed.attempt.equity_after
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        return worst


@dataclass(frozen=True, slots=True)
class _PendingOrder:
    context: PortfolioContext
    authority: object
    opportunity: Opportunity
    intent: OrderIntent
    replay: ReplayResult
    priority: tuple[object, ...]
    fill_at: pd.Timestamp | None
    cancel_at: pd.Timestamp | None
    cancel_reason: str | None

    def __post_init__(self) -> None:
        if (self.fill_at is None) == (self.cancel_at is None):
            raise ValueError("a pending order must have exactly one terminal event")

    @property
    def event_at(self) -> pd.Timestamp:
        value = self.fill_at if self.fill_at is not None else self.cancel_at
        assert value is not None
        return value


_TARGET_KIND_RANK = {
    "pivot": 0,
    "impulse": 1,
    "order_block": 2,
    "fvg": 3,
}


def cost_aware_open_slot_priority(
    opportunity: Opportunity,
    context: PortfolioContext,
    costs: CostConfig,
) -> tuple[object, ...]:
    """Rank simultaneous fills without preferring a strategy version by name.

    Independent pivot liquidity owns the strongest target rank. Within the same
    target authority, the candidate with more cost-inclusive target room wins,
    followed by literal execution overlap and tighter entry geometry.
    """

    target_r = cost_inclusive_target_r(
        side=opportunity.side,
        entry_price=opportunity.planned_entry.price,
        stop_price=opportunity.initial_stop,
        target_price=opportunity.target.order_price,
        costs=costs,
    )
    authority = opportunity.authority
    return (
        _TARGET_KIND_RANK.get(opportunity.target.kind, 4),
        -target_r,
        0 if bool(getattr(authority, "has_literal_body_overlap", False)) else 1,
        float(authority.zone.width) / opportunity.planned_entry.price,
        context.symbol,
        str(authority.authority_id),
    )


def _terminal_pending_order(
    *,
    context: PortfolioContext,
    authority: object,
    opportunity: Opportunity,
    intent: OrderIntent,
    replay: ReplayResult,
    priority: tuple[object, ...],
) -> _PendingOrder:
    fill_at = _entry_fill_time(replay)
    expiration = _first_expiration(context, opportunity, replay)
    if expiration is not None:
        return _PendingOrder(
            context=context,
            authority=authority,
            opportunity=opportunity,
            intent=intent,
            replay=replay,
            priority=priority,
            fill_at=None,
            cancel_at=expiration[0],
            cancel_reason=expiration[1],
        )
    if replay.status == "ENTRY_REJECTED":
        if not replay.events:
            raise RuntimeError("an entry rejection must carry an event")
        return _PendingOrder(
            context=context,
            authority=authority,
            opportunity=opportunity,
            intent=intent,
            replay=replay,
            priority=priority,
            fill_at=None,
            cancel_at=replay.events[-1].occurred_at,
            cancel_reason=replay.rejection_reason or "entry_rejected",
        )
    if fill_at is not None:
        return _PendingOrder(
            context=context,
            authority=authority,
            opportunity=opportunity,
            intent=intent,
            replay=replay,
            priority=priority,
            fill_at=fill_at,
            cancel_at=None,
            cancel_reason=None,
        )
    # A score boundary normally owns this path through _first_expiration. Keep a
    # defensive terminal event so malformed input cannot create an immortal order.
    return _PendingOrder(
        context=context,
        authority=authority,
        opportunity=opportunity,
        intent=intent,
        replay=replay,
        priority=priority,
        fill_at=None,
        cancel_at=context.score_end,
        cancel_reason="score_window_ended_before_entry",
    )


def _result(
    *,
    contexts: tuple[PortfolioContext, ...],
    closed: list[ClosedPortfolioAttempt],
    initial_equity: float,
    final_equity: float,
    opportunity_rejections: int,
    sizing_rejections: int,
    exposure_rejections: int,
    pending_cancellations: int,
    entry_rejections: int,
    open_censored: int,
    entry_censored: int,
    slot_suppressed_authorities: int,
    simultaneous_candidate_cutoffs: int,
    simultaneous_candidates: int,
    pending_orders_created: int,
    cross_cancelled_pending: int,
    maximum_concurrent_pending: int,
) -> OpenSlotPortfolioReplayResult:
    return OpenSlotPortfolioReplayResult(
        contexts=contexts,
        closed_attempts=tuple(closed),
        initial_equity=initial_equity,
        final_equity=final_equity,
        opportunity_rejections=opportunity_rejections,
        sizing_rejections=sizing_rejections,
        exposure_rejections=exposure_rejections,
        pending_cancellations=pending_cancellations,
        entry_rejections=entry_rejections,
        open_censored=open_censored,
        entry_censored=entry_censored,
        slot_suppressed_authorities=slot_suppressed_authorities,
        simultaneous_candidate_cutoffs=simultaneous_candidate_cutoffs,
        simultaneous_candidates=simultaneous_candidates,
        pending_orders_created=pending_orders_created,
        cross_cancelled_pending=cross_cancelled_pending,
        maximum_concurrent_pending=maximum_concurrent_pending,
    )


def run_open_slot_portfolio(
    contexts: Sequence[PortfolioContext],
    *,
    initial_equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    assemble_candidate: CandidateAssembler,
    maximum_notional_to_equity: float = 8.0,
    candidate_priority: OpenSlotCandidatePriority = cost_aware_open_slot_priority,
) -> OpenSlotPortfolioReplayResult:
    """Replay many pending limits with one shared BTC/ETH open-position slot.

    While flat, every causally valid limit may wait independently. The order with
    the earliest actual fill wins the slot; all siblings are cancelled at that
    fill timestamp. Authorities born while a position is already open remain
    suppressed, because their first-return history cannot be reconstructed as a
    fresh live order without look-ahead. This removes artificial pending-order
    blocking without ever allowing two open positions.
    """

    ordered_contexts = tuple(contexts)
    if not ordered_contexts:
        raise ValueError("at least one portfolio context is required")
    equity = float(initial_equity)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("initial_equity must be finite and positive")
    exposure_cap = float(maximum_notional_to_equity)
    if not math.isfinite(exposure_cap) or exposure_cap <= 0:
        raise ValueError("maximum_notional_to_equity must be finite and positive")

    by_cutoff = _candidate_cutoffs(ordered_contexts)
    cutoffs = sorted(by_cutoff)
    cutoff_index = 0
    flat_at = cutoffs[0] if cutoffs else min(
        context.score_start for context in ordered_contexts
    )
    pending: list[_PendingOrder] = []
    closed: list[ClosedPortfolioAttempt] = []
    opportunity_rejections = 0
    sizing_rejections = 0
    exposure_rejections = 0
    pending_cancellations = 0
    entry_rejections = 0
    open_censored = 0
    entry_censored = 0
    slot_suppressed = 0
    simultaneous_cutoffs = 0
    simultaneous_candidates = 0
    pending_created = 0
    cross_cancelled = 0
    maximum_pending = 0

    while cutoff_index < len(cutoffs) or pending:
        while cutoff_index < len(cutoffs) and cutoffs[cutoff_index] < flat_at:
            slot_suppressed += len(by_cutoff[cutoffs[cutoff_index]])
            cutoff_index += 1

        next_cutoff = (
            None if cutoff_index >= len(cutoffs) else cutoffs[cutoff_index]
        )
        next_pending_event = (
            None if not pending else min(order.event_at for order in pending)
        )

        if next_cutoff is not None and (
            next_pending_event is None or next_cutoff <= next_pending_event
        ):
            raw = by_cutoff[next_cutoff]
            opportunities: list[tuple[Opportunity, PortfolioContext, object]] = []
            for context, authority in raw:
                assembled = assemble_candidate(context.book, authority, costs)
                if isinstance(assembled, OpportunityRejection):
                    opportunity_rejections += 1
                    continue
                opportunities.append((assembled, context, authority))
            if len(opportunities) > 1:
                simultaneous_cutoffs += 1
                simultaneous_candidates += len(opportunities)
            opportunities.sort(
                key=lambda item: candidate_priority(item[0], item[1], costs)
            )

            for opportunity, context, authority in opportunities:
                try:
                    intent = intent_from_opportunity(
                        opportunity,
                        equity=equity,
                        costs=costs,
                        risk=risk,
                        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
                    )
                except ValueError:
                    sizing_rejections += 1
                    continue
                planned_notional = intent.quantity * intent.entry_reference
                if planned_notional / equity > exposure_cap + 1e-12:
                    exposure_rejections += 1
                    continue
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
                order = _terminal_pending_order(
                    context=context,
                    authority=authority,
                    opportunity=opportunity,
                    intent=intent,
                    replay=replay,
                    priority=candidate_priority(opportunity, context, costs),
                )
                if order.event_at < next_cutoff:
                    raise RuntimeError("pending terminal event precedes its authority")
                if order.event_at == next_cutoff and order.fill_at is None:
                    if order.replay.status == "ENTRY_REJECTED":
                        entry_rejections += 1
                    else:
                        pending_cancellations += 1
                    continue
                pending.append(order)
                pending_created += 1
            maximum_pending = max(maximum_pending, len(pending))
            cutoff_index += 1
            continue

        if next_pending_event is None:
            break

        cancellations = [
            order
            for order in pending
            if order.cancel_at is not None and order.cancel_at == next_pending_event
        ]
        if cancellations:
            for order in cancellations:
                if order.replay.status == "ENTRY_REJECTED":
                    entry_rejections += 1
                else:
                    pending_cancellations += 1
            cancelled_ids = {id(order) for order in cancellations}
            pending = [order for order in pending if id(order) not in cancelled_ids]

        fills = [
            order
            for order in pending
            if order.fill_at is not None and order.fill_at == next_pending_event
        ]
        if not fills:
            continue
        selected = min(fills, key=lambda order: order.priority)
        cross_cancelled += len(pending) - 1
        pending.clear()

        before = equity
        replay = selected.replay
        if replay.trade is not None:
            if not (
                selected.context.score_start
                <= replay.trade.entry_time
                < selected.context.score_end
            ):
                raise RuntimeError("a completed trade entered outside its score window")
            equity += replay.trade.net_pnl
            attempt = ReplayAttempt(
                opportunity_id=selected.opportunity.opportunity_id,
                authority_id=selected.authority.authority_id,
                intent=selected.intent,
                result=replay,
                equity_before=before,
                equity_after=equity,
            )
            closed.append(
                ClosedPortfolioAttempt(
                    context=selected.context,
                    authority=selected.authority,
                    attempt=attempt,
                )
            )
            flat_at = replay.trade.closed_at
            continue

        if replay.status == "OPEN_CENSORED":
            open_censored += 1
        else:
            entry_censored += 1
        # The equity and slot after an unresolved fill are unknown. Do not resume
        # in a later yearly window and manufacture a continuation path.
        slot_suppressed += sum(
            len(by_cutoff[cutoff]) for cutoff in cutoffs[cutoff_index:]
        )
        return _result(
            contexts=ordered_contexts,
            closed=closed,
            initial_equity=float(initial_equity),
            final_equity=equity,
            opportunity_rejections=opportunity_rejections,
            sizing_rejections=sizing_rejections,
            exposure_rejections=exposure_rejections,
            pending_cancellations=pending_cancellations,
            entry_rejections=entry_rejections,
            open_censored=open_censored,
            entry_censored=entry_censored,
            slot_suppressed_authorities=slot_suppressed,
            simultaneous_candidate_cutoffs=simultaneous_cutoffs,
            simultaneous_candidates=simultaneous_candidates,
            pending_orders_created=pending_created,
            cross_cancelled_pending=cross_cancelled,
            maximum_concurrent_pending=maximum_pending,
        )

    return _result(
        contexts=ordered_contexts,
        closed=closed,
        initial_equity=float(initial_equity),
        final_equity=equity,
        opportunity_rejections=opportunity_rejections,
        sizing_rejections=sizing_rejections,
        exposure_rejections=exposure_rejections,
        pending_cancellations=pending_cancellations,
        entry_rejections=entry_rejections,
        open_censored=open_censored,
        entry_censored=entry_censored,
        slot_suppressed_authorities=slot_suppressed,
        simultaneous_candidate_cutoffs=simultaneous_cutoffs,
        simultaneous_candidates=simultaneous_candidates,
        pending_orders_created=pending_created,
        cross_cancelled_pending=cross_cancelled,
        maximum_concurrent_pending=maximum_pending,
    )


__all__ = [
    "OpenSlotCandidatePriority",
    "OpenSlotPortfolioReplayResult",
    "cost_aware_open_slot_priority",
    "run_open_slot_portfolio",
]
