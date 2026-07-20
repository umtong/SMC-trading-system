from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Literal, TypeAlias

import pandas as pd

from .domain import (
    B1Subtype,
    ConfluenceAuthority,
    EntryMode,
    FairValueGap,
    FormationBar,
    LiquidityEvent,
    OrderBlock,
    OBCausalState,
    SceneAuthority,
    SceneFamily,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
    TriggerAuthority,
)
from .features import (
    TIMEFRAME_DELTA,
    detect_fvgs,
    detect_order_blocks,
    detect_strict_pivots,
    impulse_is_consumed,
    pivot_is_consumed,
    resample_all_completed,
    zone_is_consumed,
)
from .strategy import (
    SimpleExecutionCosts,
    build_m15_liquidity_m5_delivery_confirmation,
    build_target_candidates,
    compose_a1_b1_confluence,
    confluence_entry_price,
    detect_b1_liquidity_events,
    select_initial_target,
    select_preferred_confluence,
)


class StructureState(str, Enum):
    UP = "up"
    DOWN = "down"
    RANGE = "range"


class DeliveryDecision(str, Enum):
    LONG_DELIVERY = "long_delivery"
    SHORT_DELIVERY = "short_delivery"
    NO_TRADE = "no_trade"


RejectionReason = Literal["no_target", "target_space_conflict"]


@dataclass(frozen=True, slots=True)
class FeatureBook:
    symbol: str
    tick_size: float
    frames: dict[Timeframe, pd.DataFrame]
    order_blocks: dict[Timeframe, tuple[OrderBlock, ...]]
    pivots: dict[Timeframe, tuple[StrictPivot, ...]]
    fvgs: dict[Timeframe, tuple[FairValueGap, ...]]
    liquidity_events: dict[Timeframe, tuple[LiquidityEvent, ...]]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not math.isfinite(self.tick_size) or self.tick_size <= 0:
            raise ValueError("tick_size must be finite and positive")
        required = set(Timeframe)
        for name, mapping in (
            ("frames", self.frames),
            ("order_blocks", self.order_blocks),
            ("pivots", self.pivots),
            ("fvgs", self.fvgs),
        ):
            if set(mapping) != required:
                raise ValueError(f"{name} must contain every V0 timeframe")
        if set(self.liquidity_events) != {Timeframe.M5, Timeframe.M15}:
            raise ValueError("liquidity_events must contain 5m and 15m")


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    as_of: pd.Timestamp
    h1: StructureState
    h4: StructureState
    delivery: DeliveryDecision


@dataclass(frozen=True, slots=True)
class PlannedEntry:
    price: float
    available_at: pd.Timestamp
    mode: EntryMode = EntryMode.LIMIT_FIRST_REVISIT
    ob_causal_state: OBCausalState = OBCausalState.PREEXISTING

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("planned entry price must be finite and positive")
        if self.mode is EntryMode.NEXT_BAR_OPEN and self.ob_causal_state is not OBCausalState.EVENT_CREATED:
            raise ValueError("next-bar-open requires an event-created OB")
        object.__setattr__(self, "available_at", _as_of(self.available_at))


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    symbol: str
    side: Side
    authority: SceneAuthority
    planned_entry: PlannedEntry
    initial_stop: float
    target: TargetCandidate
    known_at: pd.Timestamp

    @property
    def scene_family(self) -> SceneFamily:
        return self.authority.scene_family

    @property
    def authority_id(self) -> str:
        return self.authority.authority_id


@dataclass(frozen=True, slots=True)
class OpportunityRejection:
    symbol: str
    side: Side
    authority: SceneAuthority
    reason: RejectionReason

    @property
    def scene_family(self) -> SceneFamily:
        return self.authority.scene_family

    @property
    def authority_id(self) -> str:
        return self.authority.authority_id


OpportunityResult: TypeAlias = Opportunity | OpportunityRejection


def _as_of(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError("as_of must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _frame_as_of(
    book: FeatureBook, timeframe: Timeframe, as_of: pd.Timestamp
) -> pd.DataFrame:
    frame = book.frames[timeframe]
    closes = frame.index + TIMEFRAME_DELTA[timeframe]
    return frame.loc[closes <= as_of]


def _values_after(
    frame: pd.DataFrame,
    *,
    timeframe: Timeframe,
    after: pd.Timestamp,
    column: str,
):
    close_times = frame.index + TIMEFRAME_DELTA[timeframe]
    start = int(close_times.searchsorted(after, side="right"))
    return frame[column].to_numpy(dtype=float, copy=False)[start:]


def build_feature_book(
    candles_5m: pd.DataFrame, *, symbol: str, tick_size: float
) -> FeatureBook:
    if not symbol:
        raise ValueError("symbol is required")
    if not math.isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be finite and positive")
    frames = resample_all_completed(candles_5m)
    order_blocks = {
        timeframe: detect_order_blocks(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            tick_size=tick_size,
        )
        for timeframe, frame in frames.items()
    }
    pivots = {
        timeframe: detect_strict_pivots(
            frame, symbol=symbol, timeframe=timeframe
        )
        for timeframe, frame in frames.items()
    }
    fvgs = {
        timeframe: detect_fvgs(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            tick_size=tick_size,
        )
        for timeframe, frame in frames.items()
    }
    event_timeframe = Timeframe.M15
    liquidity_events = {
        Timeframe.M5: (),
        Timeframe.M15: detect_b1_liquidity_events(
            pivots[Timeframe.H1],
            timeframe=event_timeframe,
            bars=tuple(
                FormationBar(
                    open_time=opened,
                    close_time=opened + TIMEFRAME_DELTA[event_timeframe],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                for opened, row in frames[event_timeframe].iterrows()
            ),
            tick_size=tick_size,
        ),
    }
    return FeatureBook(
        symbol=symbol,
        tick_size=float(tick_size),
        frames=frames,
        order_blocks=order_blocks,
        pivots=pivots,
        fvgs=fvgs,
        liquidity_events=liquidity_events,
    )


def structure_state(
    book: FeatureBook, timeframe: Timeframe, *, as_of: object
) -> StructureState:
    cutoff = _as_of(as_of)
    known = [pivot for pivot in book.pivots[timeframe] if pivot.known_at <= cutoff]
    highs = sorted(
        (pivot for pivot in known if pivot.kind == "high"),
        key=lambda pivot: (pivot.pivot_time, pivot.pivot_id),
    )
    lows = sorted(
        (pivot for pivot in known if pivot.kind == "low"),
        key=lambda pivot: (pivot.pivot_time, pivot.pivot_id),
    )
    if len(highs) < 2 or len(lows) < 2:
        return StructureState.RANGE
    previous_high, latest_high = highs[-2:]
    previous_low, latest_low = lows[-2:]
    if latest_high.price > previous_high.price and latest_low.price > previous_low.price:
        return StructureState.UP
    if latest_high.price < previous_high.price and latest_low.price < previous_low.price:
        return StructureState.DOWN
    return StructureState.RANGE


def structure_snapshot(book: FeatureBook, *, as_of: object) -> StructureSnapshot:
    cutoff = _as_of(as_of)
    h1 = structure_state(book, Timeframe.H1, as_of=cutoff)
    h4 = structure_state(book, Timeframe.H4, as_of=cutoff)
    if h1 is StructureState.UP and h4 is not StructureState.DOWN:
        delivery = DeliveryDecision.LONG_DELIVERY
    elif h1 is StructureState.DOWN and h4 is not StructureState.UP:
        delivery = DeliveryDecision.SHORT_DELIVERY
    else:
        delivery = DeliveryDecision.NO_TRADE
    return StructureSnapshot(as_of=cutoff, h1=h1, h4=h4, delivery=delivery)


def _allowed_sides(book: FeatureBook, *, as_of: pd.Timestamp) -> frozenset[Side]:
    """Apply the directional guard while keeping H1 range-edge trades possible."""

    snapshot = structure_snapshot(book, as_of=as_of)
    if snapshot.h1 is StructureState.UP and snapshot.h4 is not StructureState.DOWN:
        return frozenset({Side.LONG})
    if snapshot.h1 is StructureState.DOWN and snapshot.h4 is not StructureState.UP:
        return frozenset({Side.SHORT})
    if snapshot.h1 is StructureState.RANGE:
        return frozenset({Side.LONG, Side.SHORT})
    return frozenset()


def _range_event_is_at_boundary(
    book: FeatureBook, event: LiquidityEvent
) -> bool:
    known = [
        pivot
        for pivot in book.pivots[Timeframe.H1]
        if pivot.known_at <= event.known_at
    ]
    highs = sorted(
        (pivot for pivot in known if pivot.kind == "high"),
        key=lambda pivot: (pivot.pivot_time, pivot.pivot_id),
    )
    lows = sorted(
        (pivot for pivot in known if pivot.kind == "low"),
        key=lambda pivot: (pivot.pivot_time, pivot.pivot_id),
    )
    if not highs or not lows:
        return False
    expected = (
        lows[-1]
        if event.subtype is B1Subtype.SWEEP_RECLAIM and event.side is Side.LONG
        else highs[-1]
        if event.subtype is B1Subtype.SWEEP_RECLAIM
        else highs[-1]
        if event.side is Side.LONG
        else lows[-1]
    )
    return expected.pivot_id == event.node_id


def _event_direction_is_allowed(book: FeatureBook, event: LiquidityEvent) -> bool:
    snapshot = structure_snapshot(book, as_of=event.known_at)
    if snapshot.h1 is StructureState.UP:
        return event.side is Side.LONG and snapshot.h4 is not StructureState.DOWN
    if snapshot.h1 is StructureState.DOWN:
        return event.side is Side.SHORT and snapshot.h4 is not StructureState.UP
    return _range_event_is_at_boundary(book, event)


def _event_episode_is_valid(
    book: FeatureBook, event: LiquidityEvent, *, until: pd.Timestamp
) -> bool:
    frame = book.frames[event.timeframe]
    close_times = frame.index + TIMEFRAME_DELTA[event.timeframe]
    later = frame.loc[(close_times > event.known_at) & (close_times <= until), "close"]
    if event.side is Side.LONG:
        return not bool((later <= event.node_price - book.tick_size + 1e-12).any())
    return not bool((later >= event.node_price + book.tick_size - 1e-12).any())


def _order_block_is_active(
    book: FeatureBook, block: OrderBlock, *, as_of: pd.Timestamp
) -> bool:
    frame = _frame_as_of(book, block.timeframe, as_of)
    later = _values_after(
        frame,
        timeframe=block.timeframe,
        after=block.known_at,
        column="low" if block.side is Side.LONG else "high",
    )
    if block.side is Side.LONG:
        return not bool((later <= block.initial_stop).any())
    return not bool((later >= block.initial_stop).any())


def enumerate_b1_confirmations(
    book: FeatureBook, *, as_of: object
) -> tuple[TriggerAuthority, ...]:
    """Return causal 15m-liquidity -> 5m-MSS body-engulf confirmations.

    A 5m OB is not a confirmation merely because it appears after an event.
    Its final directional formation bar must itself close through the latest
    already-confirmed 5m swing.  This makes the OB the owner of the observed
    displacement rather than an unrelated later pattern.
    """

    cutoff = _as_of(as_of)
    blocks = tuple(
        sorted(
            (
                block
                for block in book.order_blocks[Timeframe.M5]
                if block.known_at <= cutoff
            ),
            key=lambda block: (block.known_at, block.ob_id),
        )
    )
    first_pairs: list[tuple[TriggerAuthority, LiquidityEvent]] = []
    for event in book.liquidity_events[Timeframe.M15]:
        if event.known_at > cutoff or not _event_direction_is_allowed(book, event):
            continue
        for block in blocks:
            if block.side is not event.side:
                continue
            displacement_open = block.formation_bars[-1].open_time
            if displacement_open < event.known_at:
                continue
            if not _event_episode_is_valid(book, event, until=block.known_at):
                break
            confirmation = build_m15_liquidity_m5_delivery_confirmation(
                block,
                event=event,
                pivots=book.pivots[Timeframe.M5],
                tick_size=book.tick_size,
            )
            if confirmation is None:
                continue
            first_pairs.append((confirmation, event))
            break

    pairs_by_block: dict[str, tuple[TriggerAuthority, LiquidityEvent]] = {}
    for confirmation, event in first_pairs:
        block_id = confirmation.order_blocks[0].ob_id
        current = pairs_by_block.get(block_id)
        if current is None or (event.known_at, event.event_id) > (
            current[1].known_at,
            current[1].event_id,
        ):
            pairs_by_block[block_id] = (confirmation, event)
    confirmations = tuple(
        confirmation
        for confirmation, _event in pairs_by_block.values()
        if _order_block_is_active(
            book, confirmation.order_blocks[0], as_of=cutoff
        )
    )
    return tuple(
        sorted(confirmations, key=lambda item: (item.known_at, item.authority_id))
    )
def build_confluence_authorities(
    book: FeatureBook, *, as_of: object
) -> tuple[ConfluenceAuthority, ...]:
    """Build H1/H4 context + 15m liquidity + 5m delivery-OB scenes."""

    cutoff = _as_of(as_of)
    allowed_sides = _allowed_sides(book, as_of=cutoff)
    if not allowed_sides:
        return ()
    locations = {
        timeframe: tuple(
            block
            for block in book.order_blocks[timeframe]
            if block.side in allowed_sides
            and block.known_at <= cutoff
            and _order_block_is_active(book, block, as_of=cutoff)
        )
        for timeframe in (Timeframe.H1, Timeframe.M15)
    }
    confirmations = enumerate_b1_confirmations(book, as_of=cutoff)
    events_by_id = {
        event.event_id: event
        for event in book.liquidity_events[Timeframe.M15]
        if event.known_at <= cutoff
    }
    pivots_by_id = {
        pivot.pivot_id: pivot
        for pivot in book.pivots[Timeframe.H1]
        if pivot.known_at <= cutoff
    }
    output: list[ConfluenceAuthority] = []
    for confirmation in confirmations:
        if confirmation.side not in allowed_sides:
            continue
        event = events_by_id.get(confirmation.liquidity_event_id)
        if event is None:
            continue
        pivot_location = pivots_by_id.get(confirmation.liquidity_node_id)
        if pivot_location is not None:
            authority = compose_a1_b1_confluence(
                pivot_location,
                confirmation,
                tick_size=book.tick_size,
            )
            if authority is not None:
                output.append(authority)
        location_timeframes = (
            (Timeframe.H1, Timeframe.M15)
            if confirmation.timeframes == (Timeframe.M5,)
            else (Timeframe.H1,)
        )
        for location_timeframe in location_timeframes:
            for location in locations[location_timeframe]:
                # An OB can locate the event only if it already existed when
                # that event became knowable. A location born during the
                # later 5m delivery is part of the reaction, not its A1
                # context. Strict-pivot ownership above is unchanged.
                if location.known_at > event.known_at:
                    continue
                authority = compose_a1_b1_confluence(
                    location,
                    confirmation,
                    tick_size=book.tick_size,
                )
                if authority is not None:
                    output.append(authority)
    return tuple(
        sorted(output, key=lambda item: (item.known_at, item.authority_id))
    )


def select_current_confluence(
    book: FeatureBook,
    *,
    as_of: object,
    excluded_authority_ids: frozenset[str] = frozenset(),
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> ConfluenceAuthority | None:
    cutoff = _as_of(as_of)
    allowed_sides = _allowed_sides(book, as_of=cutoff)
    if not allowed_sides:
        return None
    available = tuple(
        authority
        for authority in build_confluence_authorities(book, as_of=cutoff)
        if authority.authority_id not in excluded_authority_ids
        and _first_revisit_is_available(
            book,
            authority,
            as_of=cutoff,
            entry_mode=_entry_mode_for_authority(
                authority,
                event_created_entry_mode=event_created_entry_mode,
            ),
        )
    )
    selected_by_side = tuple(
        selected
        for side in allowed_sides
        if (
            selected := select_preferred_confluence(
                available,
                symbol=book.symbol,
                side=side,
            )
        )
        is not None
    )
    if not selected_by_side:
        return None
    return min(
        selected_by_side,
        key=lambda authority: (
            0
            if authority.location.timeframe is Timeframe.M15
            and authority.confirmation.timeframes == (Timeframe.M5,)
            else 1
            if authority.confirmation.timeframes == (Timeframe.M15,)
            else 2,
            0 if authority.has_literal_body_overlap else 1,
            authority.zone.width,
            -authority.known_at.value,
            authority.authority_id,
        ),
    )


def _first_revisit_is_available(
    book: FeatureBook,
    authority: ConfluenceAuthority,
    *,
    as_of: pd.Timestamp,
    entry_mode: EntryMode | None = None,
) -> bool:
    """Keep only the still-actionable clock for the selected causal state."""

    selected_mode = authority.entry_mode if entry_mode is None else EntryMode(entry_mode)
    if selected_mode is EntryMode.NEXT_BAR_OPEN:
        return as_of == authority.known_at
    timeframe = authority.confirmation.timeframes[0]
    frame = _frame_as_of(book, timeframe, as_of)
    later = _values_after(
        frame,
        timeframe=timeframe,
        after=authority.known_at,
        column="low" if authority.side is Side.LONG else "high",
    )
    entry = confluence_entry_price(authority)
    if authority.side is Side.LONG:
        return not bool((later <= entry).any())
    return not bool((later >= entry).any())


def _entry_mode_for_authority(
    authority: ConfluenceAuthority,
    *,
    event_created_entry_mode: EntryMode,
) -> EntryMode:
    """Apply the configurable arm only to OBs born from their event."""

    if authority.ob_causal_state is OBCausalState.PREEXISTING:
        return EntryMode.LIMIT_FIRST_REVISIT
    return EntryMode(event_created_entry_mode)


def _active_target_pivots(
    book: FeatureBook, authority: ConfluenceAuthority, *, as_of: pd.Timestamp
) -> tuple[StrictPivot, ...]:
    """Return pivots whose liquidity has not already been traded through."""

    target_kind = "high" if authority.side is Side.LONG else "low"
    owner_timeframes = {
        timeframe
        for timeframe in (
            authority.location.timeframe,
            authority.confirmation.liquidity_event_timeframe,
            *authority.confirmation.timeframes,
        )
        if timeframe in {Timeframe.M5, Timeframe.M15}
    }
    allowed_timeframes = owner_timeframes | {Timeframe.H1, Timeframe.H4}
    return tuple(
        pivot
        for timeframe in allowed_timeframes
        for pivot in book.pivots[timeframe]
        if pivot.kind == target_kind
        and pivot.known_at <= as_of
        and not pivot_is_consumed(
            pivot,
            _frame_as_of(book, pivot.timeframe, as_of),
            tick_size=book.tick_size,
        )
    )


def _active_target_order_blocks(
    book: FeatureBook, *, side: Side, as_of: pd.Timestamp
) -> tuple[OrderBlock, ...]:
    """Keep target OBs until price closes completely through their zone."""

    return tuple(
        block
        for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        for block in book.order_blocks[timeframe]
        if block.side is not side
        and block.known_at <= as_of
        and not zone_is_consumed(
            block.zone,
            _frame_as_of(book, block.timeframe, as_of),
            travel_side=side,
            timeframe=block.timeframe,
            tick_size=book.tick_size,
            after=block.known_at,
        )
    )


def _active_target_fvgs(
    book: FeatureBook, *, side: Side, as_of: pd.Timestamp
) -> tuple[FairValueGap, ...]:
    """Keep target FVGs until price closes completely through their zone."""

    return tuple(
        gap
        for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        for gap in book.fvgs[timeframe]
        if gap.side is not side
        and gap.known_at <= as_of
        and not zone_is_consumed(
            gap.zone,
            _frame_as_of(book, gap.timeframe, as_of),
            travel_side=side,
            timeframe=gap.timeframe,
            tick_size=book.tick_size,
            after=gap.known_at,
        )
    )


def _execution_block(authority: ConfluenceAuthority) -> OrderBlock:
    """Recover the OB that owns the confluence stop and impulse levels."""

    confirmation = authority.confirmation.order_blocks[0]
    location = authority.location
    if (
        isinstance(location, OrderBlock)
        and location.timeframe is Timeframe.M15
        and confirmation.timeframe is Timeframe.M5
        and math.isclose(authority.initial_stop, location.initial_stop)
        and math.isclose(authority.impulse_extreme, location.impulse_extreme)
    ):
        return location
    return confirmation


def assemble_opportunity(
    book: FeatureBook,
    authority: ConfluenceAuthority,
    *,
    as_of: object,
    costs: SimpleExecutionCosts = SimpleExecutionCosts(),
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> OpportunityResult:
    cutoff = _as_of(as_of)
    if authority.known_at > cutoff:
        raise ValueError("confluence is not yet known")
    entry = confluence_entry_price(authority)
    execution_block = _execution_block(authority)
    excluded_source_ids = (
        frozenset({authority.authority_id})
        if impulse_is_consumed(
            execution_block,
            _frame_as_of(book, execution_block.timeframe, authority.known_at),
            tick_size=book.tick_size,
        )
        else frozenset()
    )
    candidates = build_target_candidates(
        authority,
        pivots=_active_target_pivots(
            book, authority, as_of=authority.known_at
        ),
        order_blocks=_active_target_order_blocks(
            book, side=authority.side, as_of=authority.known_at
        ),
        fvgs=_active_target_fvgs(
            book, side=authority.side, as_of=authority.known_at
        ),
        as_of=authority.known_at,
        excluded_source_ids=excluded_source_ids,
    )
    selection = select_initial_target(
        candidates,
        side=authority.side,
        entry_price=entry,
        tick_size=book.tick_size,
        costs=costs,
    )
    if selection.target is None:
        reason: RejectionReason = (
            "target_space_conflict"
            if selection.rejection_reason == "target_space_conflict"
            else "no_target"
        )
        return OpportunityRejection(
            symbol=book.symbol,
            side=authority.side,
            authority=authority,
            reason=reason,
        )
    return Opportunity(
        opportunity_id=f"opportunity:{authority.authority_id}",
        symbol=book.symbol,
        side=authority.side,
        authority=authority,
        planned_entry=PlannedEntry(
            entry,
            authority.known_at,
            mode=_entry_mode_for_authority(
                authority,
                event_created_entry_mode=event_created_entry_mode,
            ),
            ob_causal_state=authority.ob_causal_state,
        ),
        initial_stop=authority.initial_stop,
        target=selection.target,
        known_at=authority.known_at,
    )


def assemble_confluence_opportunities(
    book: FeatureBook,
    *,
    as_of: object,
    costs: SimpleExecutionCosts = SimpleExecutionCosts(),
    excluded_authority_ids: frozenset[str] = frozenset(),
    event_created_entry_mode: EntryMode = EntryMode.NEXT_BAR_OPEN,
) -> tuple[OpportunityResult, ...]:
    """Return at most one current two-OB scene; A1 or B1 alone returns none."""

    authority = select_current_confluence(
        book,
        as_of=as_of,
        excluded_authority_ids=excluded_authority_ids,
        event_created_entry_mode=event_created_entry_mode,
    )
    if authority is None:
        return ()
    return (
        assemble_opportunity(
            book,
            authority,
            as_of=as_of,
            costs=costs,
            event_created_entry_mode=event_created_entry_mode,
        ),
    )


__all__ = [
    "DeliveryDecision",
    "FeatureBook",
    "Opportunity",
    "OpportunityRejection",
    "OpportunityResult",
    "PlannedEntry",
    "StructureSnapshot",
    "StructureState",
    "assemble_confluence_opportunities",
    "assemble_opportunity",
    "build_confluence_authorities",
    "build_feature_book",
    "enumerate_b1_confirmations",
    "select_current_confluence",
    "structure_snapshot",
    "structure_state",
]






