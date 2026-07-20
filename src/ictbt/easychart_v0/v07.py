from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import math

import pandas as pd

from .application import (
    DailyLossBlock,
    DailyLossGuard,
    PendingCancellation,
    ReplayAttempt,
    SizingRejection,
    intent_from_opportunity,
)
from .domain import (
    B1Subtype,
    ConfluenceAuthority,
    EntryMode,
    FairValueGap,
    FormationBar,
    LiquidityEvent,
    OBCausalState,
    PriceZone,
    SceneFamily,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .execution import CostConfig, RiskConfig
from .features import pivot_is_consumed, zone_is_consumed
from .pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    PlannedEntry,
    _frame_as_of,
    _order_block_is_active,
    assemble_opportunity as assemble_v03_opportunity,
    build_feature_book,
)
from .replay import replay_intent
from .strategy import SimpleExecutionCosts, estimated_net_pnl, select_initial_target
from .v04 import (
    V04HistoricalReplayRun,
    _authority_priority as _legacy_authority_priority,
    _preentry_expiration,
    _target_costs,
    assemble_v04_opportunity,
)


class V07ExecutionArm(str, Enum):
    FIRST_RETURN_LIMIT = "first_return_limit"
    BOUNDARY_ACCEPT_NEXT_OPEN = "boundary_accept_next_open"


@dataclass(frozen=True, slots=True)
class SrFlipFvgAuthority:
    """A pre-known H1/M15 boundary flipped by a fresh M15 FVG's B-bar."""

    authority_id: str
    scene_root_id: str
    symbol: str
    side: Side
    boundary_pivot: StrictPivot
    liquidity_event: LiquidityEvent
    fvg: FairValueGap
    break_bar: FormationBar
    acceptance_bar: FormationBar
    zone: PriceZone
    known_at: pd.Timestamp
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    destination: TargetCandidate

    def __post_init__(self) -> None:
        if not self.authority_id or not self.scene_root_id or not self.symbol:
            raise ValueError("SR-flip/FVG authority identity fields are required")
        if (
            self.boundary_pivot.symbol != self.symbol
            or self.liquidity_event.symbol != self.symbol
            or self.fvg.symbol != self.symbol
        ):
            raise ValueError("SR-flip/FVG symbol mismatch")
        if self.boundary_pivot.timeframe not in {Timeframe.M15, Timeframe.H1}:
            raise ValueError("SR-flip boundary must belong to M15 or H1")
        if (
            self.fvg.timeframe is not Timeframe.M15
            or self.liquidity_event.timeframe is not Timeframe.M15
        ):
            raise ValueError("SR-flip/FVG event and FVG must belong to M15")
        if self.fvg.side is not self.side or self.liquidity_event.side is not self.side:
            raise ValueError("SR-flip/FVG side mismatch")
        expected_kind = "high" if self.side is Side.LONG else "low"
        if self.boundary_pivot.kind != expected_kind:
            raise ValueError("SR-flip boundary is on the wrong side")
        if self.boundary_pivot.known_at > self.break_bar.open_time:
            raise ValueError("SR-flip boundary must be known before the break bar opens")
        if self.break_bar != self.fvg.formation_bars[1]:
            raise ValueError("the FVG B-bar must own the boundary break")
        if self.acceptance_bar != self.fvg.formation_bars[2]:
            raise ValueError("the FVG C-bar must own boundary acceptance")
        if not (
            math.isclose(self.zone.low, self.boundary_pivot.price)
            and math.isclose(self.zone.high, self.boundary_pivot.price)
        ):
            raise ValueError("the execution zone must be the flipped boundary line")
        if not _boundary_is_in_fvg(self.fvg, boundary=self.boundary_pivot.price):
            raise ValueError("the flipped boundary must lie inside the fresh FVG")
        known = _utc(self.known_at, name="known_at")
        if known != self.fvg.known_at or known != self.acceptance_bar.close_time:
            raise ValueError("SR-flip/FVG authority is known at the C-bar close")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        expected_extreme = (
            min(bar.low for bar in self.fvg.formation_bars)
            if self.side is Side.LONG
            else max(bar.high for bar in self.fvg.formation_bars)
        )
        if not math.isclose(stop_extreme, expected_extreme):
            raise ValueError("SR-flip/FVG stop must be owned by its formation")
        limit_entry = self.zone.high if self.side is Side.LONG else self.zone.low
        if (self.side is Side.LONG and not initial_stop < limit_entry) or (
            self.side is Side.SHORT and not initial_stop > limit_entry
        ):
            raise ValueError("SR-flip/FVG stop is on the wrong side of entry")
        if self.destination.trade_side is not self.side:
            raise ValueError("SR-flip/FVG destination side mismatch")
        if self.destination.known_at > known:
            raise ValueError("SR-flip/FVG destination must exist before the order")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)

    @property
    def scene_family(self) -> SceneFamily:
        return SceneFamily.SR_FLIP_FVG

    @property
    def ob_causal_state(self) -> OBCausalState:
        return OBCausalState.EVENT_CREATED

    @property
    def location_id(self) -> str:
        return self.boundary_pivot.pivot_id

    @property
    def execution_id(self) -> str:
        return self.fvg.fvg_id

    @property
    def has_literal_body_overlap(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class V07BuildDiagnostics:
    m15_fvgs: int
    preconfirmed_boundaries: int
    directional_breaks: int
    accepted_breaks: int
    boundary_linked_fvgs: int
    targets_missing_at_acceptance: int
    duplicate_scenes_suppressed: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V07BuildResult:
    authorities: tuple[SrFlipFvgAuthority, ...]
    diagnostics: V07BuildDiagnostics


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _positive(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _active_boundaries(
    book: FeatureBook,
    *,
    side: Side,
    break_bar: FormationBar,
) -> tuple[StrictPivot, ...]:
    kind = "high" if side is Side.LONG else "low"
    active: list[StrictPivot] = []
    for timeframe in (Timeframe.M15, Timeframe.H1):
        frame = _frame_as_of(book, timeframe, break_bar.open_time)
        active.extend(
            pivot
            for pivot in book.pivots[timeframe]
            if pivot.symbol == book.symbol
            and pivot.kind == kind
            and pivot.known_at <= break_bar.open_time
            and not pivot_is_consumed(pivot, frame, tick_size=book.tick_size)
        )
    return tuple(active)


def _boundary_preference(pivot: StrictPivot) -> tuple[int, int, int, str]:
    """Prefer M15 precision, then the newest already-confirmed boundary."""

    return (
        0 if pivot.timeframe is Timeframe.M15 else 1,
        -pivot.pivot_time.value,
        -pivot.known_at.value,
        pivot.pivot_id,
    )


def _directional_break(
    *,
    side: Side,
    a_bar: FormationBar,
    break_bar: FormationBar,
    boundary: float,
    tick_size: float,
) -> bool:
    if side is Side.LONG:
        return (
            break_bar.bullish
            and a_bar.close <= boundary + 1e-12
            and break_bar.close >= boundary + tick_size - 1e-12
        )
    return (
        break_bar.bearish
        and a_bar.close >= boundary - 1e-12
        and break_bar.close <= boundary - tick_size + 1e-12
    )


def _accepted(
    *,
    side: Side,
    acceptance_bar: FormationBar,
    boundary: float,
    tick_size: float,
) -> bool:
    return (
        acceptance_bar.close >= boundary + tick_size - 1e-12
        if side is Side.LONG
        else acceptance_bar.close <= boundary - tick_size + 1e-12
    )


def _boundary_is_in_fvg(
    gap: FairValueGap,
    *,
    boundary: float,
) -> bool:
    return gap.zone.low - 1e-12 <= boundary <= gap.zone.high + 1e-12


def _nearest_destination_at_scene(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    as_of: pd.Timestamp,
    excluded_source_ids: frozenset[str],
) -> TargetCandidate | None:
    """Freeze the nearest active opposing structure known at scene completion."""

    candidates: list[TargetCandidate] = []
    target_pivot_kind = "high" if side is Side.LONG else "low"
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, as_of)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != target_pivot_kind
                or pivot.known_at > as_of
                or pivot.pivot_id in excluded_source_ids
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:pivot:{pivot.pivot_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="pivot",
                    zone=PriceZone(pivot.price, pivot.price),
                    known_at=pivot.known_at,
                    source_id=pivot.pivot_id,
                )
            )

        for block in book.order_blocks[timeframe]:
            if (
                block.symbol != book.symbol
                or block.side is side
                or block.known_at > as_of
                or block.ob_id in excluded_source_ids
                or not _order_block_is_active(book, block, as_of=as_of)
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:ob:{block.ob_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="order_block",
                    zone=block.zone,
                    known_at=block.known_at,
                    source_id=block.ob_id,
                )
            )

        for gap in book.fvgs[timeframe]:
            if (
                gap.symbol != book.symbol
                or gap.side is side
                or gap.known_at > as_of
                or gap.fvg_id in excluded_source_ids
                or zone_is_consumed(
                    gap.zone,
                    frame,
                    travel_side=side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=gap.known_at,
                )
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:fvg:{gap.fvg_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="fvg",
                    zone=gap.zone,
                    known_at=gap.known_at,
                    source_id=gap.fvg_id,
                )
            )

    selection = select_initial_target(
        candidates,
        side=side,
        entry_price=entry_price,
        tick_size=book.tick_size,
    )
    return selection.target


def build_v07_scene_family_result(book: FeatureBook) -> V07BuildResult:
    """Build each SR-flip/FVG scene once, independently of execution arm.

    The FVG's B-bar must break an active H1/M15 pivot known at its open.  All
    boundaries are tested against the complete break/acceptance/FVG condition
    before M15 precision and recency choose one.  The C close is the single
    scene clock shared by both execution arms.
    """

    raw: list[SrFlipFvgAuthority] = []
    preconfirmed = 0
    directional = 0
    accepted = 0
    linked = 0
    targets_missing = 0

    for gap in book.fvgs[Timeframe.M15]:
        a_bar, break_bar, acceptance_bar = gap.formation_bars
        boundaries = _active_boundaries(
            book,
            side=gap.side,
            break_bar=break_bar,
        )
        if not boundaries:
            continue
        preconfirmed += 1
        broken = tuple(
            boundary
            for boundary in boundaries
            if _directional_break(
                side=gap.side,
                a_bar=a_bar,
                break_bar=break_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
        )
        if not broken:
            continue
        directional += 1
        accepted_boundaries = tuple(
            boundary
            for boundary in broken
            if _accepted(
                side=gap.side,
                acceptance_bar=acceptance_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
        )
        if not accepted_boundaries:
            continue
        accepted += 1
        linked_boundaries = tuple(
            boundary
            for boundary in accepted_boundaries
            if _boundary_is_in_fvg(gap, boundary=boundary.price)
        )
        if not linked_boundaries:
            continue
        linked += 1
        boundary = min(linked_boundaries, key=_boundary_preference)

        root_id = (
            f"v07-sr-flip-fvg:{book.symbol}:{gap.side.value}:"
            f"{break_bar.open_time.isoformat()}"
        )
        event_extreme = (
            min(bar.low for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else max(bar.high for bar in gap.formation_bars)
        )
        event = LiquidityEvent(
            event_id=f"{root_id}:boundary-acceptance",
            symbol=book.symbol,
            timeframe=Timeframe.M15,
            subtype=B1Subtype.BREAK_RETEST,
            side=gap.side,
            node_id=boundary.pivot_id,
            node_price=boundary.price,
            event_time=break_bar.open_time,
            known_at=acceptance_bar.close_time,
            event_extreme=event_extreme,
        )
        destination = _nearest_destination_at_scene(
            book,
            side=gap.side,
            entry_price=boundary.price,
            as_of=gap.known_at,
            excluded_source_ids=frozenset({boundary.pivot_id, gap.fvg_id}),
        )
        if destination is None:
            targets_missing += 1
            continue

        stop_extreme = event_extreme
        initial_stop = (
            stop_extreme - book.tick_size
            if gap.side is Side.LONG
            else stop_extreme + book.tick_size
        )
        impulse_extreme = (
            max(bar.high for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else min(bar.low for bar in gap.formation_bars)
        )
        raw.append(
            SrFlipFvgAuthority(
                authority_id=(
                    f"{root_id}|boundary={boundary.pivot_id}|fvg={gap.fvg_id}"
                ),
                scene_root_id=root_id,
                symbol=book.symbol,
                side=gap.side,
                boundary_pivot=boundary,
                liquidity_event=event,
                fvg=gap,
                break_bar=break_bar,
                acceptance_bar=acceptance_bar,
                zone=PriceZone(boundary.price, boundary.price),
                known_at=gap.known_at,
                stop_extreme=stop_extreme,
                initial_stop=initial_stop,
                impulse_extreme=impulse_extreme,
                destination=destination,
            )
        )

    grouped: dict[str, list[SrFlipFvgAuthority]] = {}
    for authority in raw:
        grouped.setdefault(authority.scene_root_id, []).append(authority)
    selected = [
        min(items, key=lambda authority: authority.authority_id)
        for items in grouped.values()
    ]
    authorities = tuple(
        sorted(selected, key=lambda authority: (authority.known_at, authority.authority_id))
    )
    return V07BuildResult(
        authorities=authorities,
        diagnostics=V07BuildDiagnostics(
            m15_fvgs=len(book.fvgs[Timeframe.M15]),
            preconfirmed_boundaries=preconfirmed,
            directional_breaks=directional,
            accepted_breaks=accepted,
            boundary_linked_fvgs=linked,
            targets_missing_at_acceptance=targets_missing,
            duplicate_scenes_suppressed=len(raw) - len(authorities),
            authorities=len(authorities),
        ),
    )


def build_v07_scene_family_authorities(
    book: FeatureBook,
) -> tuple[SrFlipFvgAuthority, ...]:
    return build_v07_scene_family_result(book).authorities


def _entry_mode(arm: V07ExecutionArm | str) -> EntryMode:
    selected = V07ExecutionArm(arm)
    return (
        EntryMode.LIMIT_FIRST_REVISIT
        if selected is V07ExecutionArm.FIRST_RETURN_LIMIT
        else EntryMode.NEXT_BAR_OPEN
    )


def _planned_entry_price(
    authority: SrFlipFvgAuthority,
    arm: V07ExecutionArm,
) -> float:
    # NEXT_BAR_OPEN is repriced and resized only when that open is actually
    # available.  Until then the flipped boundary is a neutral scene reference;
    # the C close must not become an extra, unapproved profitability filter.
    return (
        authority.zone.high
        if authority.side is Side.LONG
        else authority.zone.low
    )


def assemble_v07_opportunity(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
    entry_arm: V07ExecutionArm | str,
) -> Opportunity | OpportunityRejection:
    arm = V07ExecutionArm(entry_arm)
    entry = _planned_entry_price(authority, arm)
    selection = select_initial_target(
        (authority.destination,),
        side=authority.side,
        entry_price=entry,
        tick_size=book.tick_size,
        costs=(
            _target_costs(costs)
            if arm is V07ExecutionArm.FIRST_RETURN_LIMIT
            else SimpleExecutionCosts()
        ),
    )
    if selection.target is None:
        return OpportunityRejection(
            symbol=book.symbol,
            side=authority.side,
            authority=authority,  # type: ignore[arg-type]
            reason=(
                "target_space_conflict"
                if selection.rejection_reason == "target_space_conflict"
                else "no_target"
            ),
        )
    return Opportunity(
        opportunity_id=f"opportunity:{authority.authority_id}:{arm.value}",
        symbol=book.symbol,
        side=authority.side,
        authority=authority,  # type: ignore[arg-type]
        planned_entry=PlannedEntry(
            price=entry,
            available_at=authority.known_at,
            mode=_entry_mode(arm),
            ob_causal_state=OBCausalState.EVENT_CREATED,
        ),
        initial_stop=authority.initial_stop,
        target=selection.target,
        known_at=authority.known_at,
    )


def _next_m5_open(
    candles_5m: pd.DataFrame,
    *,
    after: pd.Timestamp,
) -> tuple[pd.Timestamp, float] | None:
    later = candles_5m.loc[candles_5m.index >= after]
    if later.empty:
        return None
    opened = later.index[0]
    return opened, float(later.iloc[0]["open"])


def _next_open_is_cost_positive(
    opportunity: Opportunity,
    *,
    actual_open: float,
    costs: CostConfig,
) -> bool:
    return estimated_net_pnl(
        side=opportunity.side,
        entry_price=actual_open,
        exit_price=opportunity.target.order_price,
        quantity=1.0,
        costs=_target_costs(costs),
    ) > 0


def _next_open_has_valid_geometry(
    opportunity: Opportunity,
    *,
    actual_open: float,
) -> bool:
    return (
        opportunity.initial_stop < actual_open < opportunity.target.order_price
        if opportunity.side is Side.LONG
        else opportunity.target.order_price
        < actual_open
        < opportunity.initial_stop
    )


def _authority_entry_mode(
    authority: object,
    *,
    v07_arm: V07ExecutionArm,
) -> EntryMode:
    if isinstance(authority, SrFlipFvgAuthority):
        return _entry_mode(v07_arm)
    return EntryMode.LIMIT_FIRST_REVISIT


def run_v07_historical_replay(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    tick_size: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    entry_arm: V07ExecutionArm | str,
    book: FeatureBook | None = None,
    authorities: tuple[object, ...] | None = None,
    use_v03_targets: bool | None = None,
) -> V04HistoricalReplayRun:
    """Replay V0.7 alone or with V0.3/V0.5 under the existing one slot.

    ``entry_arm`` changes only :class:`SrFlipFvgAuthority`.  Every other
    authority keeps the locked first-return behavior used by the leader arm.
    """

    arm = V07ExecutionArm(entry_arm)
    feature_book = (
        build_feature_book(candles_5m, symbol=symbol, tick_size=tick_size)
        if book is None
        else book
    )
    selected_authorities = (
        tuple(build_v07_scene_family_result(feature_book).authorities)
        if authorities is None
        else tuple(authorities)
    )
    grouped: dict[pd.Timestamp, list[object]] = {}
    for authority in selected_authorities:
        grouped.setdefault(authority.known_at, []).append(authority)

    current_equity = float(equity)
    attempts: list[ReplayAttempt] = []
    rejections: list[OpportunityRejection] = []
    sizing_rejections: list[SizingRejection] = []
    cancellations: list[PendingCancellation] = []
    daily_blocks: list[DailyLossBlock] = []
    recorded_days: set[date] = set()
    occupied_until: pd.Timestamp | None = None
    slot_suppressed_authorities = 0
    daily_guard = DailyLossGuard(risk)

    for cutoff in sorted(grouped):
        if occupied_until is not None and cutoff < occupied_until:
            slot_suppressed_authorities += len(grouped[cutoff])
            continue
        status = daily_guard.status(at=cutoff, equity=current_equity)
        if status.blocked:
            if status.local_date not in recorded_days:
                daily_blocks.append(
                    DailyLossBlock(
                        decision_at=cutoff,
                        local_date=status.local_date,
                        day_start_equity=status.day_start_equity,
                        realized_net_pnl=status.realized_net_pnl,
                        loss_limit_cash=status.loss_limit_cash,
                    )
                )
                recorded_days.add(status.local_date)
            continue

        accepted: Opportunity | None = None
        accepted_authority: object | None = None
        for authority in sorted(grouped[cutoff], key=_legacy_authority_priority):
            if isinstance(authority, SrFlipFvgAuthority):
                result = assemble_v07_opportunity(
                    feature_book,
                    authority,
                    costs=costs,
                    entry_arm=arm,
                )
            else:
                dynamic_v03 = isinstance(authority, ConfluenceAuthority) and (
                    use_v03_targets is True
                    or (use_v03_targets is None and authority.destination is None)
                )
                result = (
                    assemble_v03_opportunity(
                        feature_book,
                        authority,
                        as_of=authority.known_at,
                        costs=_target_costs(costs),
                        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
                    )
                    if dynamic_v03
                    else assemble_v04_opportunity(
                        feature_book,
                        authority,
                        costs=costs,
                    )
                )
            if isinstance(result, OpportunityRejection):
                rejections.append(result)
                continue
            if (
                isinstance(authority, SrFlipFvgAuthority)
                and arm is V07ExecutionArm.BOUNDARY_ACCEPT_NEXT_OPEN
            ):
                next_open = _next_m5_open(candles_5m, after=cutoff)
                if next_open is None:
                    rejections.append(
                        OpportunityRejection(
                            symbol=feature_book.symbol,
                            side=result.side,
                            authority=authority,  # type: ignore[arg-type]
                            reason="next_bar_open_unavailable",  # type: ignore[arg-type]
                        )
                    )
                    continue
                if not _next_open_has_valid_geometry(
                    result,
                    actual_open=next_open[1],
                ):
                    rejections.append(
                        OpportunityRejection(
                            symbol=feature_book.symbol,
                            side=result.side,
                            authority=authority,  # type: ignore[arg-type]
                            reason="next_bar_open_outside_trade_geometry",  # type: ignore[arg-type]
                        )
                    )
                    continue
                if not _next_open_is_cost_positive(
                    result,
                    actual_open=next_open[1],
                    costs=costs,
                ):
                    rejections.append(
                        OpportunityRejection(
                            symbol=feature_book.symbol,
                            side=result.side,
                            authority=authority,  # type: ignore[arg-type]
                            reason="next_bar_open_not_cost_positive_to_fixed_target",  # type: ignore[arg-type]
                        )
                    )
                    continue
            accepted = result
            accepted_authority = authority
            break
        if accepted is None or accepted_authority is None:
            continue

        selected_mode = _authority_entry_mode(
            accepted_authority,
            v07_arm=arm,
        )
        effective_risk = daily_guard.risk_for_new_order(
            at=cutoff,
            equity=current_equity,
        )
        try:
            intent = intent_from_opportunity(
                accepted,
                equity=current_equity,
                costs=costs,
                risk=effective_risk,
                event_created_entry_mode=selected_mode,
            )
        except ValueError as exc:
            sizing_rejections.append(
                SizingRejection(accepted.opportunity_id, str(exc))
            )
            continue

        replay = replay_intent(
            intent,
            candles=candles_5m,
            candle_interval="5min",
            costs=costs,
            volume_bars={
                Timeframe.M5: candles_5m,
                Timeframe.M15: feature_book.frames[Timeframe.M15],
            },
        )
        expiration = _preentry_expiration(feature_book, accepted, replay)
        if expiration is not None:
            cancellations.append(
                PendingCancellation(
                    opportunity_id=accepted.opportunity_id,
                    authority_id=accepted.authority_id,
                    order_id=intent.order_id,
                    cancelled_at=expiration[0],
                    reason=expiration[1],
                )
            )
            occupied_until = expiration[0]
            continue

        before = current_equity
        if replay.trade is not None:
            current_equity += replay.trade.net_pnl
            daily_guard.record_realized(
                closed_at=replay.trade.closed_at,
                net_pnl=replay.trade.net_pnl,
                equity_before=before,
            )
        attempts.append(
            ReplayAttempt(
                opportunity_id=accepted.opportunity_id,
                authority_id=accepted.authority_id,
                intent=intent,
                result=replay,
                equity_before=before,
                equity_after=current_equity,
            )
        )
        if replay.trade is not None:
            occupied_until = replay.trade.closed_at
        elif replay.status == "ENTRY_REJECTED":
            occupied_until = replay.events[-1].occurred_at
        else:
            break

    return V04HistoricalReplayRun(
        book=feature_book,
        authorities=selected_authorities,  # type: ignore[arg-type]
        attempts=tuple(attempts),
        opportunity_rejections=tuple(rejections),
        sizing_rejections=tuple(sizing_rejections),
        pending_cancellations=tuple(cancellations),
        daily_loss_blocks=tuple(daily_blocks),
        slot_suppressed_authorities=slot_suppressed_authorities,
        initial_equity=float(equity),
        final_equity=current_equity,
    )


__all__ = [
    "SrFlipFvgAuthority",
    "V07BuildDiagnostics",
    "V07BuildResult",
    "V07ExecutionArm",
    "assemble_v07_opportunity",
    "build_v07_scene_family_authorities",
    "build_v07_scene_family_result",
    "run_v07_historical_replay",
]
