from __future__ import annotations

from dataclasses import dataclass
import math
from types import SimpleNamespace
from typing import Callable, Iterable, Sequence

import pandas as pd

from .application import ReplayAttempt, intent_from_opportunity
from .domain import EntryMode, Side, Timeframe
from .execution import CostConfig, RiskConfig
from .pipeline import FeatureBook, Opportunity, OpportunityRejection
from .replay import ReplayResult, replay_intent
from .v04 import _preentry_expiration


Authority = object
CandidateAssembler = Callable[
    [FeatureBook, Authority, CostConfig], Opportunity | OpportunityRejection
]
CandidatePriority = Callable[[Opportunity, "PortfolioContext"], tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class PortfolioContext:
    context_id: str
    symbol: str
    candles: pd.DataFrame
    book: FeatureBook
    authorities: tuple[Authority, ...]
    score_start: pd.Timestamp
    score_end: pd.Timestamp
    data_end: pd.Timestamp

    def __post_init__(self) -> None:
        if not self.context_id or not self.symbol:
            raise ValueError("portfolio context identity is required")
        if self.book.symbol != self.symbol:
            raise ValueError("portfolio context symbol mismatch")
        for name in ("score_start", "score_end", "data_end"):
            timestamp = pd.Timestamp(getattr(self, name))
            if pd.isna(timestamp) or timestamp.tz is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, timestamp.tz_convert("UTC"))
        if not self.score_start < self.score_end <= self.data_end:
            raise ValueError("portfolio context score/data boundaries are invalid")
        if any(authority.symbol != self.symbol for authority in self.authorities):
            raise ValueError("portfolio authority symbol mismatch")

    @property
    def scored_authorities(self) -> tuple[Authority, ...]:
        return tuple(
            authority
            for authority in self.authorities
            if self.score_start <= authority.known_at < self.score_end
        )

    @property
    def operating_dates(self) -> frozenset[pd.Timestamp]:
        final_day = (self.score_end - pd.Timedelta(nanoseconds=1)).normalize()
        return frozenset(
            pd.date_range(
                self.score_start.normalize(),
                final_day,
                freq="1D",
                tz="UTC",
            )
        )


@dataclass(frozen=True, slots=True)
class ClosedPortfolioAttempt:
    context: PortfolioContext
    authority: Authority
    attempt: ReplayAttempt


@dataclass(frozen=True, slots=True)
class PortfolioReplayResult:
    contexts: tuple[PortfolioContext, ...]
    closed_attempts: tuple[ClosedPortfolioAttempt, ...]
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


def _planned_target_r(opportunity: Opportunity) -> float:
    entry = opportunity.planned_entry.price
    stop = opportunity.initial_stop
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    direction = 1.0 if opportunity.side is Side.LONG else -1.0
    return direction * (opportunity.target.order_price - entry) / risk


def default_candidate_priority(
    opportunity: Opportunity,
    context: PortfolioContext,
) -> tuple[object, ...]:
    """Prefer explicit V0.8 causal scenes, then efficient clean geometry."""

    authority = opportunity.authority
    authority_id = str(authority.authority_id)
    return (
        0 if authority_id.startswith("v08-") else 1,
        0 if bool(getattr(authority, "has_literal_body_overlap", False)) else 1,
        -_planned_target_r(opportunity),
        float(authority.zone.width) / opportunity.planned_entry.price,
        context.symbol,
        authority_id,
    )


def _entry_fill_time(replay: ReplayResult) -> pd.Timestamp | None:
    if replay.trade is not None:
        return replay.trade.entry_time
    if replay.open_position is not None:
        return replay.open_position.filled_at
    return None


def _score_boundary_expiration(
    context: PortfolioContext,
    replay: ReplayResult,
) -> tuple[pd.Timestamp, str] | None:
    fill_time = _entry_fill_time(replay)
    if fill_time is not None and fill_time < context.score_end:
        return None
    return context.score_end, "score_window_ended_before_entry"


def _first_expiration(
    context: PortfolioContext,
    opportunity: Opportunity,
    replay: ReplayResult,
) -> tuple[pd.Timestamp, str] | None:
    choices = tuple(
        item
        for item in (
            _preentry_expiration(context.book, opportunity, replay),
            _score_boundary_expiration(context, replay),
        )
        if item is not None
    )
    if not choices:
        return None
    expiration = min(choices, key=lambda item: (item[0], item[1]))
    fill_time = _entry_fill_time(replay)
    if fill_time is not None and expiration[0] >= fill_time:
        return None
    return expiration


def _candidate_cutoffs(
    contexts: Iterable[PortfolioContext],
) -> dict[pd.Timestamp, list[tuple[PortfolioContext, Authority]]]:
    output: dict[pd.Timestamp, list[tuple[PortfolioContext, Authority]]] = {}
    for context in contexts:
        for authority in context.scored_authorities:
            output.setdefault(authority.known_at, []).append((context, authority))
    return output


def run_global_portfolio(
    contexts: Sequence[PortfolioContext],
    *,
    initial_equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    assemble_candidate: CandidateAssembler,
    candidate_priority: CandidatePriority = default_candidate_priority,
) -> PortfolioReplayResult:
    """Replay BTC/ETH on one chronological equity path and one occupied slot."""

    ordered_contexts = tuple(contexts)
    if not ordered_contexts:
        raise ValueError("at least one portfolio context is required")
    equity = float(initial_equity)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("initial_equity must be finite and positive")
    candidates_by_cutoff = _candidate_cutoffs(ordered_contexts)
    occupied_until: pd.Timestamp | None = None
    closed: list[ClosedPortfolioAttempt] = []
    opportunity_rejections = 0
    sizing_rejections = 0
    pending_cancellations = 0
    entry_rejections = 0
    open_censored = 0
    entry_censored = 0
    slot_suppressed = 0
    simultaneous_cutoffs = 0
    simultaneous_candidates = 0

    for cutoff in sorted(candidates_by_cutoff):
        raw = candidates_by_cutoff[cutoff]
        if occupied_until is not None and cutoff < occupied_until:
            slot_suppressed += len(raw)
            continue

        opportunities: list[tuple[Opportunity, PortfolioContext, Authority]] = []
        for context, authority in raw:
            result = assemble_candidate(context.book, authority, costs)
            if isinstance(result, OpportunityRejection):
                opportunity_rejections += 1
                continue
            opportunities.append((result, context, authority))
        if len(opportunities) > 1:
            simultaneous_cutoffs += 1
            simultaneous_candidates += len(opportunities)
        if not opportunities:
            continue
        opportunities.sort(key=lambda item: candidate_priority(item[0], item[1]))

        selected: tuple[Opportunity, PortfolioContext, Authority] | None = None
        intent = None
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
            selected = opportunity, context, authority
            break
        if selected is None or intent is None:
            continue

        opportunity, context, authority = selected
        before = equity
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
        expiration = _first_expiration(context, opportunity, replay)
        if expiration is not None:
            pending_cancellations += 1
            occupied_until = expiration[0]
            continue
        if replay.status == "ENTRY_REJECTED":
            entry_rejections += 1
            occupied_until = replay.events[-1].occurred_at
            continue
        if replay.trade is not None:
            if not (
                context.score_start
                <= replay.trade.entry_time
                < context.score_end
            ):
                raise RuntimeError("a completed trade entered outside its score window")
            equity += replay.trade.net_pnl
            attempt = ReplayAttempt(
                opportunity_id=opportunity.opportunity_id,
                authority_id=authority.authority_id,
                intent=intent,
                result=replay,
                equity_before=before,
                equity_after=equity,
            )
            closed.append(
                ClosedPortfolioAttempt(
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
        # Never manufacture a close at the supplied data boundary.  The trial
        # remains invalid and the unresolved order owns the slot through data_end.
        occupied_until = context.data_end

    return PortfolioReplayResult(
        contexts=ordered_contexts,
        closed_attempts=tuple(closed),
        initial_equity=float(initial_equity),
        final_equity=equity,
        opportunity_rejections=opportunity_rejections,
        sizing_rejections=sizing_rejections,
        pending_cancellations=pending_cancellations,
        entry_rejections=entry_rejections,
        open_censored=open_censored,
        entry_censored=entry_censored,
        slot_suppressed_authorities=slot_suppressed,
        simultaneous_candidate_cutoffs=simultaneous_cutoffs,
        simultaneous_candidates=simultaneous_candidates,
    )


__all__ = [
    "CandidateAssembler",
    "CandidatePriority",
    "ClosedPortfolioAttempt",
    "PortfolioContext",
    "PortfolioReplayResult",
    "default_candidate_priority",
    "run_global_portfolio",
]
