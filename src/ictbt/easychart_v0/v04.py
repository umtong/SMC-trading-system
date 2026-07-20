from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

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
    LiquidityDeliveryAuthority,
    LiquidityEvent,
    OBCausalState,
    OrderBlock,
    PriceZone,
    SceneAuthority,
    SceneFamily,
    Side,
    StrictPivot,
    StructureFlipAuthority,
    TargetCandidate,
    Timeframe,
)
from .execution import CostConfig, RiskConfig, TradeRecord
from .features import TIMEFRAME_DELTA, pivot_is_consumed, zone_is_consumed
from .pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    OpportunityResult,
    PlannedEntry,
    StructureState,
    _allowed_sides,
    _event_direction_is_allowed,
    _event_episode_is_valid,
    _frame_as_of,
    _order_block_is_active,
    assemble_opportunity as assemble_v03_opportunity,
    build_confluence_authorities,
    build_feature_book,
)
from .replay import ReplayResult, replay_intent
from .strategy import (
    SimpleExecutionCosts,
    build_m15_liquidity_m5_delivery_confirmation,
    compose_a1_b1_confluence,
    select_initial_target,
)


@dataclass(frozen=True, slots=True)
class V04Policy:
    include_corrected_event: bool = True
    include_preexisting_structure: bool = True


@dataclass(frozen=True, slots=True)
class V04HistoricalReplayRun:
    book: FeatureBook
    authorities: tuple[SceneAuthority, ...]
    attempts: tuple[ReplayAttempt, ...]
    opportunity_rejections: tuple[OpportunityRejection, ...]
    sizing_rejections: tuple[SizingRejection, ...]
    pending_cancellations: tuple[PendingCancellation, ...]
    daily_loss_blocks: tuple[DailyLossBlock, ...]
    slot_suppressed_authorities: int
    initial_equity: float
    final_equity: float

    @property
    def closed_trades(self) -> tuple[TradeRecord, ...]:
        return tuple(
            attempt.result.trade
            for attempt in self.attempts
            if attempt.result.trade is not None
        )


def _target_costs(costs: CostConfig) -> SimpleExecutionCosts:
    return SimpleExecutionCosts(
        entry_fee_rate=costs.entry_fee_rate,
        exit_fee_rate=costs.target_fee_rate,
    )


def _bar(opened: pd.Timestamp, row: pd.Series, timeframe: Timeframe) -> FormationBar:
    return FormationBar(
        open_time=opened,
        close_time=opened + TIMEFRAME_DELTA[timeframe],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def authority_entry_price(authority: SceneAuthority) -> float:
    return authority.zone.high if authority.side is Side.LONG else authority.zone.low


def _ahead(side: Side, price: float, reference: float, tick: float) -> bool:
    return (
        price >= reference + tick - 1e-12
        if side is Side.LONG
        else price <= reference - tick + 1e-12
    )


def _target_touched(
    book: FeatureBook,
    target: TargetCandidate,
    *,
    after: pd.Timestamp,
    through: pd.Timestamp,
) -> bool:
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    bars = frame.loc[(closes > after) & (closes <= through)]
    if target.trade_side is Side.LONG:
        return bool((bars["high"] >= target.order_price).any())
    return bool((bars["low"] <= target.order_price).any())


def _event_destination(
    book: FeatureBook,
    authority: ConfluenceAuthority,
) -> TargetCandidate | None:
    event = next(
        (
            item
            for item in book.liquidity_events[Timeframe.M15]
            if item.event_id == authority.confirmation.liquidity_event_id
        ),
        None,
    )
    if event is None:
        return None

    side = authority.side
    pivot_kind = "high" if side is Side.LONG else "low"
    pivot_timeframes = (
        (Timeframe.M15, Timeframe.H1)
        if event.subtype is B1Subtype.SWEEP_RECLAIM
        else (Timeframe.H1, Timeframe.H4)
    )
    candidates: list[TargetCandidate] = []
    for timeframe in pivot_timeframes:
        frame = _frame_as_of(book, timeframe, event.known_at)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.kind != pivot_kind
                or pivot.known_at > event.known_at
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
                or not _ahead(side, pivot.price, event.node_price, book.tick_size)
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"event-destination:pivot:{pivot.pivot_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="pivot",
                    zone=PriceZone(pivot.price, pivot.price),
                    known_at=event.known_at,
                    source_id=pivot.pivot_id,
                )
            )

    if not candidates:
        for timeframe in (Timeframe.H1, Timeframe.H4):
            frame = _frame_as_of(book, timeframe, event.known_at)
            for block in book.order_blocks[timeframe]:
                if (
                    block.side is side
                    or block.known_at > event.known_at
                    or zone_is_consumed(
                        block.zone,
                        frame,
                        travel_side=side,
                        timeframe=timeframe,
                        tick_size=book.tick_size,
                        after=block.known_at,
                    )
                ):
                    continue
                target = TargetCandidate(
                    candidate_id=f"event-destination:ob:{block.ob_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="order_block",
                    zone=block.zone,
                    known_at=event.known_at,
                    source_id=block.ob_id,
                )
                if _ahead(side, target.order_price, event.node_price, book.tick_size):
                    candidates.append(target)
            for gap in book.fvgs[timeframe]:
                if (
                    gap.side is side
                    or gap.known_at > event.known_at
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
                target = TargetCandidate(
                    candidate_id=f"event-destination:fvg:{gap.fvg_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="fvg",
                    zone=gap.zone,
                    known_at=event.known_at,
                    source_id=gap.fvg_id,
                )
                if _ahead(side, target.order_price, event.node_price, book.tick_size):
                    candidates.append(target)

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            abs(item.order_price - event.node_price),
            item.kind,
            item.candidate_id,
        ),
    )


def build_baseline_event_authorities(
    book: FeatureBook,
) -> tuple[ConfluenceAuthority, ...]:
    """Build every causal event scene once, without the historical O(N²) snapshots."""

    blocks = tuple(
        sorted(
            book.order_blocks[Timeframe.M5],
            key=lambda block: (block.known_at, block.ob_id),
        )
    )
    first_pairs: list[tuple[object, object]] = []
    for event in book.liquidity_events[Timeframe.M15]:
        if not _event_direction_is_allowed(book, event):
            continue
        for block in blocks:
            if block.side is not event.side:
                continue
            if block.formation_bars[-1].open_time < event.known_at:
                continue
            if not _event_episode_is_valid(book, event, until=block.known_at):
                break
            confirmation = build_m15_liquidity_m5_delivery_confirmation(
                block,
                event=event,
                pivots=book.pivots[Timeframe.M5],
                tick_size=book.tick_size,
            )
            if confirmation is not None:
                first_pairs.append((confirmation, event))
                break

    # A delivery OB belongs to the most recent still-valid liquidity episode.
    by_block: dict[str, tuple[object, object]] = {}
    for confirmation, event in first_pairs:
        block_id = confirmation.order_blocks[0].ob_id
        current = by_block.get(block_id)
        if current is None or (event.known_at, event.event_id) > (
            current[1].known_at,
            current[1].event_id,
        ):
            by_block[block_id] = (confirmation, event)

    output: list[ConfluenceAuthority] = []
    h1_pivots = {pivot.pivot_id: pivot for pivot in book.pivots[Timeframe.H1]}
    for confirmation, event in by_block.values():
        cutoff = confirmation.known_at
        if not _order_block_is_active(
            book,
            confirmation.order_blocks[0],
            as_of=cutoff,
        ):
            continue
        allowed = _allowed_sides(book, as_of=cutoff)
        if confirmation.side not in allowed:
            continue
        pivot = h1_pivots.get(confirmation.liquidity_node_id)
        if pivot is not None and pivot.known_at <= cutoff:
            authority = compose_a1_b1_confluence(
                pivot,
                confirmation,
                tick_size=book.tick_size,
            )
            if authority is not None:
                output.append(authority)
        for timeframe in (Timeframe.H1, Timeframe.M15):
            for location in book.order_blocks[timeframe]:
                if (
                    location.side is not confirmation.side
                    or location.known_at > event.known_at
                    or not _order_block_is_active(book, location, as_of=cutoff)
                ):
                    continue
                authority = compose_a1_b1_confluence(
                    location,
                    confirmation,
                    tick_size=book.tick_size,
                )
                if authority is not None:
                    output.append(authority)

    return tuple(sorted(output, key=lambda item: (item.known_at, item.authority_id)))


def build_corrected_event_authorities(
    book: FeatureBook,
) -> tuple[ConfluenceAuthority, ...]:
    """Freeze the event destination, but let the M5 delivery OB own the trade stop."""

    by_confirmation: dict[str, list[ConfluenceAuthority]] = {}
    for authority in build_baseline_event_authorities(book):
        destination = _event_destination(book, authority)
        if destination is None:
            continue
        event = next(
            item
            for item in book.liquidity_events[Timeframe.M15]
            if item.event_id == authority.confirmation.liquidity_event_id
        )
        if _target_touched(
            book,
            destination,
            after=event.known_at,
            through=authority.known_at,
        ):
            continue
        execution_ob = authority.confirmation.order_blocks[0]
        corrected = replace(
            authority,
            stop_extreme=execution_ob.stop_extreme,
            initial_stop=execution_ob.initial_stop,
            impulse_extreme=execution_ob.impulse_extreme,
            destination=destination,
        )
        by_confirmation.setdefault(
            authority.confirmation.authority_id, []
        ).append(corrected)

    selected: list[ConfluenceAuthority] = []
    for authorities in by_confirmation.values():
        selected.append(
            min(
                authorities,
                key=lambda item: (
                    0
                    if item.location.timeframe is Timeframe.M15
                    and item.confirmation.timeframes == (Timeframe.M5,)
                    else 1,
                    0 if item.has_literal_body_overlap else 1,
                    item.zone.width,
                    -item.location.known_at.value,
                    item.authority_id,
                ),
            )
        )
    return tuple(sorted(selected, key=lambda item: (item.known_at, item.authority_id)))


def _ob_invalidation_time(
    book: FeatureBook,
    block: OrderBlock,
) -> pd.Timestamp | None:
    frame = book.frames[block.timeframe]
    closes = frame.index + TIMEFRAME_DELTA[block.timeframe]
    later = frame.loc[closes > block.known_at]
    invalid = (
        later["low"] <= block.initial_stop
        if block.side is Side.LONG
        else later["high"] >= block.initial_stop
    )
    hits = later.index[invalid.to_numpy()]
    return (
        None
        if len(hits) == 0
        else hits[0] + TIMEFRAME_DELTA[block.timeframe]
    )


def _refinement_for_break(
    book: FeatureBook,
    location: OrderBlock,
    *,
    break_open: pd.Timestamp,
    not_before: pd.Timestamp,
    invalidated_at: dict[str, pd.Timestamp | None],
) -> tuple[OrderBlock | None, PriceZone]:
    candidates: list[tuple[int, float, str, OrderBlock, PriceZone]] = []
    for block in book.order_blocks[Timeframe.M5]:
        if (
            block.side is not location.side
            or block.known_at < not_before
            or block.known_at > break_open
            or (
                invalidated_at[block.ob_id] is not None
                and invalidated_at[block.ob_id] <= break_open
            )
        ):
            continue
        low = max(location.zone.low, block.zone.low)
        high = min(location.zone.high, block.zone.high)
        if high - low + 1e-12 < book.tick_size:
            continue
        intersection = PriceZone(low, high)
        candidates.append(
            (
                -block.known_at.value,
                intersection.width,
                block.ob_id,
                block,
                intersection,
            )
        )
    if not candidates:
        return None, location.zone
    _, _, _, block, intersection = min(candidates)
    return block, intersection


def _latest_break_pivot(
    book: FeatureBook,
    *,
    side: Side,
    at_open: pd.Timestamp,
) -> StrictPivot | None:
    kind = "high" if side is Side.LONG else "low"
    known = [
        pivot
        for pivot in book.pivots[Timeframe.M5]
        if pivot.kind == kind and pivot.known_at <= at_open
    ]
    return (
        None
        if not known
        else max(known, key=lambda item: (item.pivot_time, item.pivot_id))
    )


def _detect_m5_sweeps_of_m15_liquidity(
    book: FeatureBook,
) -> tuple[LiquidityEvent, ...]:
    """Detect completed 5m sweep/reclaims of already-confirmed M15 pivots."""

    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    events: list[LiquidityEvent] = []
    for pivot in book.pivots[Timeframe.M15]:
        start = max(1, int(frame.index.searchsorted(pivot.known_at, side="left")))
        for index in range(start, len(frame)):
            opened = frame.index[index]
            if pivot.known_at > opened:
                continue
            previous_close = float(frame.iloc[index - 1]["close"])
            row = frame.iloc[index]
            if pivot.kind == "low":
                side = Side.LONG
                qualifies = (
                    previous_close > pivot.price
                    and float(row["low"]) <= pivot.price - book.tick_size + 1e-12
                    and float(row["close"]) >= pivot.price + book.tick_size - 1e-12
                )
            else:
                side = Side.SHORT
                qualifies = (
                    previous_close < pivot.price
                    and float(row["high"]) >= pivot.price + book.tick_size - 1e-12
                    and float(row["close"]) <= pivot.price - book.tick_size + 1e-12
                )
            if not qualifies:
                continue
            events.append(
                LiquidityEvent(
                    event_id=(
                        f"{book.symbol}:5m:sweep_reclaim:{side.value}:"
                        f"{pivot.pivot_id}:{opened.isoformat()}"
                    ),
                    symbol=book.symbol,
                    timeframe=Timeframe.M5,
                    subtype=B1Subtype.SWEEP_RECLAIM,
                    side=side,
                    node_id=pivot.pivot_id,
                    node_price=pivot.price,
                    event_time=opened,
                    known_at=closes[index],
                    event_extreme=(
                        float(row["low"])
                        if side is Side.LONG
                        else float(row["high"])
                    ),
                )
            )
            break
    return tuple(sorted(events, key=lambda item: (item.known_at, item.event_id)))


def _m5_sweep_episode_is_valid(
    book: FeatureBook,
    event: LiquidityEvent,
    *,
    until: pd.Timestamp,
) -> bool:
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    later = frame.loc[(closes > event.known_at) & (closes <= until)]
    if event.side is Side.LONG:
        return not bool(
            (later["close"] <= event.node_price - book.tick_size + 1e-12).any()
        )
    return not bool(
        (later["close"] >= event.node_price + book.tick_size - 1e-12).any()
    )


def _structure_location_side_is_allowed(
    book: FeatureBook,
    location: OrderBlock,
    *,
    as_of: pd.Timestamp,
) -> bool:
    """Use H1/H4 direction; require an H1 range edge only when both are ranging."""

    from .pipeline import structure_snapshot

    snapshot = structure_snapshot(book, as_of=as_of)
    if snapshot.h1 is StructureState.UP:
        return location.side is Side.LONG and snapshot.h4 is not StructureState.DOWN
    if snapshot.h1 is StructureState.DOWN:
        return location.side is Side.SHORT and snapshot.h4 is not StructureState.UP

    pivot_kind = "low" if location.side is Side.LONG else "high"
    pivots = [
        pivot
        for pivot in book.pivots[Timeframe.H1]
        if pivot.kind == pivot_kind and pivot.known_at <= as_of
    ]
    if not pivots:
        return False
    boundary = max(
        pivots,
        key=lambda item: (item.pivot_time, item.pivot_id),
    )
    return (
        location.zone.low - book.tick_size
        <= boundary.price
        <= location.zone.high + book.tick_size
    )


def build_preexisting_structure_authorities(
    book: FeatureBook,
) -> tuple[StructureFlipAuthority, ...]:
    """Build M15 location -> later M5 close-break -> first post-break retest scenes."""

    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    output: list[StructureFlipAuthority] = []
    liquidity_sweeps = _detect_m5_sweeps_of_m15_liquidity(book)
    m5_invalidated_at = {
        block.ob_id: _ob_invalidation_time(book, block)
        for block in book.order_blocks[Timeframe.M5]
    }
    m15_invalidated_at = {
        block.ob_id: _ob_invalidation_time(book, block)
        for block in book.order_blocks[Timeframe.M15]
    }
    break_candidates: dict[Side, list[tuple[int, StrictPivot]]] = {
        Side.LONG: [],
        Side.SHORT: [],
    }
    for index in range(1, len(frame)):
        opened = frame.index[index]
        previous_close = float(frame.iloc[index - 1]["close"])
        current_close = float(frame.iloc[index]["close"])
        for side in (Side.LONG, Side.SHORT):
            pivot = _latest_break_pivot(book, side=side, at_open=opened)
            if pivot is None:
                continue
            crossed = (
                previous_close <= pivot.price + 1e-12
                and current_close >= pivot.price + book.tick_size - 1e-12
                if side is Side.LONG
                else previous_close >= pivot.price - 1e-12
                and current_close <= pivot.price - book.tick_size + 1e-12
            )
            if crossed:
                break_candidates[side].append((index, pivot))

    for location in book.order_blocks[Timeframe.M15]:
        start = max(1, int(frame.index.searchsorted(location.known_at, side="left")))
        invalidated_at = m15_invalidated_at[location.ob_id]
        for index, pivot in break_candidates[location.side]:
            if index < start:
                continue
            opened = frame.index[index]
            close_time = closes[index]
            if invalidated_at is not None and invalidated_at <= close_time:
                break
            current_close = float(frame.iloc[index]["close"])
            reaction = frame.iloc[start : index + 1]
            contacts = reaction.loc[
                (reaction["low"] <= location.zone.high)
                & (reaction["high"] >= location.zone.low)
            ]
            if contacts.empty:
                continue
            contact_open = contacts.index[-1]
            contact_known_at = contact_open + TIMEFRAME_DELTA[Timeframe.M5]
            matching_sweeps = [
                event
                for event in liquidity_sweeps
                if event.side is location.side
                and location.known_at < event.known_at <= opened
                and _m5_sweep_episode_is_valid(
                    book,
                    event,
                    until=opened,
                )
                and location.zone.low - book.tick_size
                <= event.node_price
                <= location.zone.high + book.tick_size
            ]
            if not matching_sweeps:
                continue
            liquidity_event = max(
                matching_sweeps,
                key=lambda item: (item.known_at, item.event_id),
            )
            reaction_known_at = max(contact_known_at, liquidity_event.known_at)
            refinement, zone = _refinement_for_break(
                book,
                location,
                break_open=opened,
                not_before=reaction_known_at,
                invalidated_at=m5_invalidated_at,
            )
            beyond_zone = (
                current_close >= zone.high + book.tick_size - 1e-12
                if location.side is Side.LONG
                else current_close <= zone.low - book.tick_size + 1e-12
            )
            if not beyond_zone:
                continue
            if not _structure_location_side_is_allowed(
                book,
                location,
                as_of=close_time,
            ):
                continue
            execution_ob = refinement or location
            impulse_rows = frame.loc[
                (closes > execution_ob.known_at) & (closes <= close_time)
            ]
            observed_extreme = (
                float(impulse_rows["high"].max())
                if location.side is Side.LONG
                else float(impulse_rows["low"].min())
            )
            impulse_extreme = (
                max(execution_ob.impulse_extreme, observed_extreme)
                if location.side is Side.LONG
                else min(execution_ob.impulse_extreme, observed_extreme)
            )
            entry = zone.high if location.side is Side.LONG else zone.low
            if not _ahead(
                location.side,
                impulse_extreme,
                entry,
                book.tick_size,
            ):
                continue
            destination = TargetCandidate(
                candidate_id=(
                    f"structure-destination:{location.ob_id}:"
                    f"{opened.isoformat()}"
                ),
                symbol=book.symbol,
                trade_side=location.side,
                kind="impulse",
                zone=PriceZone(impulse_extreme, impulse_extreme),
                known_at=close_time,
                source_id=execution_ob.ob_id,
            )
            output.append(
                StructureFlipAuthority(
                    authority_id=(
                        f"structure-first-retest:{location.ob_id}|"
                        f"{pivot.pivot_id}|{opened.isoformat()}"
                    ),
                    symbol=book.symbol,
                    side=location.side,
                    location_ob=location,
                    refinement_ob=refinement,
                    break_pivot=pivot,
                    break_bar=_bar(opened, frame.iloc[index], Timeframe.M5),
                    zone=zone,
                    known_at=close_time,
                    stop_extreme=execution_ob.stop_extreme,
                    initial_stop=execution_ob.initial_stop,
                    impulse_extreme=impulse_extreme,
                    destination=destination,
                    liquidity_event_id=liquidity_event.event_id,
                    liquidity_node_id=liquidity_event.node_id,
                )
            )
            break

    # One orderable scene per side and break close. Prefer literal M15+M5 overlap,
    # then the narrower executable zone and the newer location.
    grouped: dict[tuple[pd.Timestamp, Side], list[StructureFlipAuthority]] = {}
    for authority in output:
        grouped.setdefault((authority.known_at, authority.side), []).append(authority)
    selected = [
        min(
            items,
            key=lambda item: (
                0 if item.refinement_ob is not None else 1,
                item.zone.width,
                -item.location_ob.known_at.value,
                item.authority_id,
            ),
        )
        for items in grouped.values()
    ]
    return tuple(sorted(selected, key=lambda item: (item.known_at, item.authority_id)))


def build_v04_authorities(
    book: FeatureBook,
    *,
    policy: V04Policy = V04Policy(),
) -> tuple[SceneAuthority, ...]:
    authorities: list[SceneAuthority] = []
    if policy.include_corrected_event:
        authorities.extend(build_corrected_event_authorities(book))
    if policy.include_preexisting_structure:
        authorities.extend(build_preexisting_structure_authorities(book))
    return tuple(
        sorted(
            authorities,
            key=lambda item: (
                item.known_at,
                0
                if item.scene_family
                is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
                else 1,
                0 if item.has_literal_body_overlap else 1,
                item.zone.width,
                item.authority_id,
            ),
        )
    )


def assemble_v04_opportunity(
    book: FeatureBook,
    authority: SceneAuthority,
    *,
    costs: CostConfig,
) -> OpportunityResult:
    entry = authority_entry_price(authority)
    destination = authority.destination
    if destination is None:
        return OpportunityRejection(
            symbol=book.symbol,
            side=authority.side,
            authority=authority,
            reason="no_target",
        )
    selection = select_initial_target(
        (destination,),
        side=authority.side,
        entry_price=entry,
        tick_size=book.tick_size,
        costs=_target_costs(costs),
    )
    if selection.target is None:
        return OpportunityRejection(
            symbol=book.symbol,
            side=authority.side,
            authority=authority,
            reason=(
                "target_space_conflict"
                if selection.rejection_reason == "target_space_conflict"
                else "no_target"
            ),
        )
    return Opportunity(
        opportunity_id=f"opportunity:{authority.authority_id}",
        symbol=book.symbol,
        side=authority.side,
        authority=authority,
        planned_entry=PlannedEntry(
            price=entry,
            available_at=authority.known_at,
            mode=EntryMode.LIMIT_FIRST_REVISIT,
            ob_causal_state=authority.ob_causal_state,
        ),
        initial_stop=authority.initial_stop,
        target=selection.target,
        known_at=authority.known_at,
    )


def _first_touch_time(
    book: FeatureBook,
    opportunity: Opportunity,
    *,
    target: bool,
) -> pd.Timestamp | None:
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    later = frame.loc[closes > opportunity.known_at]
    if target:
        matches = (
            later["high"] >= opportunity.target.order_price
            if opportunity.side is Side.LONG
            else later["low"] <= opportunity.target.order_price
        )
    else:
        matches = (
            later["low"] <= opportunity.initial_stop
            if opportunity.side is Side.LONG
            else later["high"] >= opportunity.initial_stop
        )
    hit = later.index[matches.to_numpy()]
    return None if len(hit) == 0 else hit[0] + TIMEFRAME_DELTA[Timeframe.M5]


def _entry_fill_time(replay: ReplayResult) -> pd.Timestamp | None:
    if replay.trade is not None:
        return replay.trade.entry_time
    if replay.open_position is not None:
        return replay.open_position.filled_at
    return None


def _preentry_expiration(
    book: FeatureBook,
    opportunity: Opportunity,
    replay: ReplayResult,
) -> tuple[pd.Timestamp, str] | None:
    choices = [
        (time, reason)
        for time, reason in (
            (
                _first_touch_time(book, opportunity, target=True),
                "initial_target_used_before_entry",
            ),
            (
                _first_touch_time(book, opportunity, target=False),
                "initial_stop_used_before_entry",
            ),
            (
                _first_liquidity_delivery_event_invalidation_time(
                    book,
                    opportunity.authority,
                ),
                "m5_sweep_episode_invalidated_before_entry",
            ),
            (
                _first_liquidity_delivery_location_invalidation_time(
                    book,
                    opportunity.authority,
                ),
                "m15_location_invalidated_before_entry",
            ),
        )
        if time is not None
    ]
    if not choices:
        return None
    expiration = min(choices, key=lambda item: (item[0], item[1]))
    fill_time = _entry_fill_time(replay)
    if fill_time is not None and expiration[0] >= fill_time:
        return None
    return expiration


def _first_liquidity_delivery_event_invalidation_time(
    book: FeatureBook,
    authority: SceneAuthority,
) -> pd.Timestamp | None:
    if not isinstance(authority, LiquidityDeliveryAuthority):
        return None
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    later = frame.loc[closes > authority.known_at]
    invalid = (
        later["close"]
        <= authority.liquidity_event.node_price - book.tick_size + 1e-12
        if authority.side is Side.LONG
        else later["close"]
        >= authority.liquidity_event.node_price + book.tick_size - 1e-12
    )
    hits = later.index[invalid.to_numpy()]
    return None if len(hits) == 0 else hits[0] + TIMEFRAME_DELTA[Timeframe.M5]


def _first_liquidity_delivery_location_invalidation_time(
    book: FeatureBook,
    authority: SceneAuthority,
) -> pd.Timestamp | None:
    if not isinstance(authority, LiquidityDeliveryAuthority):
        return None
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    later = frame.loc[closes > authority.known_at]
    invalid = (
        later["low"] <= authority.location_ob.initial_stop
        if authority.side is Side.LONG
        else later["high"] >= authority.location_ob.initial_stop
    )
    hits = later.index[invalid.to_numpy()]
    return None if len(hits) == 0 else hits[0] + TIMEFRAME_DELTA[Timeframe.M5]


def _authority_priority(authority: SceneAuthority) -> tuple[object, ...]:
    return (
        0
        if authority.scene_family
        is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
        else 1,
        0 if authority.has_literal_body_overlap else 1,
        authority.zone.width,
        -authority.known_at.value,
        authority.authority_id,
    )


def run_v04_historical_replay(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    tick_size: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    policy: V04Policy = V04Policy(),
    book: FeatureBook | None = None,
    authorities: tuple[SceneAuthority, ...] | None = None,
    use_v03_targets: bool | None = False,
) -> V04HistoricalReplayRun:
    book = (
        build_feature_book(candles_5m, symbol=symbol, tick_size=tick_size)
        if book is None
        else book
    )
    if authorities is None:
        authorities = (
            build_baseline_event_authorities(book)
            if use_v03_targets
            else build_v04_authorities(book, policy=policy)
        )
    grouped: dict[pd.Timestamp, list[SceneAuthority]] = {}
    for authority in authorities:
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
        for authority in sorted(grouped[cutoff], key=_authority_priority):
            use_dynamic_v03_targets = use_v03_targets is True or (
                use_v03_targets is None
                and isinstance(authority, ConfluenceAuthority)
                and authority.destination is None
            )
            result = (
                assemble_v03_opportunity(
                    book,
                    authority,
                    as_of=authority.known_at,
                    costs=_target_costs(costs),
                    event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
                )
                if use_dynamic_v03_targets
                else assemble_v04_opportunity(book, authority, costs=costs)
            )
            if isinstance(result, OpportunityRejection):
                rejections.append(result)
                continue
            accepted = result
            break
        if accepted is None:
            continue

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
                event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
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
                Timeframe.M15: book.frames[Timeframe.M15],
            },
        )
        expiration = _preentry_expiration(book, accepted, replay)
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
        book=book,
        authorities=authorities,
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
    "V04HistoricalReplayRun",
    "V04Policy",
    "assemble_v04_opportunity",
    "authority_entry_price",
    "build_baseline_event_authorities",
    "build_corrected_event_authorities",
    "build_preexisting_structure_authorities",
    "build_v04_authorities",
    "run_v04_historical_replay",
]












