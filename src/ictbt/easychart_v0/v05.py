from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .domain import (
    B1Subtype,
    DeliveryKind,
    DeliveryStopOwner,
    EntryZoneSource,
    FairValueGap,
    FormationBar,
    LiquidityDeliveryAuthority,
    LiquidityEvent,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .features import TIMEFRAME_DELTA, pivot_is_consumed, zone_is_consumed
from .pipeline import FeatureBook, _frame_as_of, _order_block_is_active
from .v04 import (
    _m5_sweep_episode_is_valid,
    _structure_location_side_is_allowed,
    _target_touched,
)


@dataclass(frozen=True, slots=True)
class _LocationSweep:
    location: OrderBlock
    pivot: StrictPivot
    event: LiquidityEvent


@dataclass(frozen=True, slots=True)
class _DeliveryCandidate:
    known_at: pd.Timestamp
    kind: DeliveryKind
    delivery_root_id: str
    pivot: StrictPivot
    order_block: OrderBlock | None
    fvg: FairValueGap | None
    zone: PriceZone
    entry_zone_source: EntryZoneSource
    stop_owner: DeliveryStopOwner
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float


@dataclass(frozen=True, slots=True)
class V05BuildDiagnostics:
    m15_locations: int
    location_pivot_pairs: int
    location_sweep_events: int
    ob_delivery_candidates: int
    fvg_delivery_candidates: int
    episodes_without_delivery: int
    targets_missing_at_event: int
    targets_used_before_delivery: int
    duplicate_scenes_suppressed: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V05BuildResult:
    authorities: tuple[LiquidityDeliveryAuthority, ...]
    diagnostics: V05BuildDiagnostics


def _ahead(side: Side, price: float, reference: float, tick: float) -> bool:
    return (
        price >= reference + tick - 1e-12
        if side is Side.LONG
        else price <= reference - tick + 1e-12
    )


def _intersection(
    left: PriceZone,
    right: PriceZone,
    *,
    minimum_width: float,
) -> PriceZone | None:
    low = max(left.low, right.low)
    high = min(left.high, right.high)
    if high - low + 1e-12 < minimum_width:
        return None
    return PriceZone(low, high)


def _refine_entry_zone(
    location: OrderBlock,
    execution_zone: PriceZone,
    *,
    base_source: EntryZoneSource,
    tick_size: float,
) -> tuple[PriceZone, EntryZoneSource]:
    overlap = _intersection(
        location.zone,
        execution_zone,
        minimum_width=tick_size,
    )
    if overlap is not None:
        return overlap, "m15_m5_intersection"
    return execution_zone, base_source


def _detect_location_sweeps(
    book: FeatureBook,
) -> tuple[tuple[_LocationSweep, ...], int]:
    """Detect the first M5 sweep after each M15-location/pivot pair exists."""

    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    raw: list[_LocationSweep] = []
    pair_count = 0

    for location in book.order_blocks[Timeframe.M15]:
        pivot_kind = "low" if location.side is Side.LONG else "high"
        for pivot in book.pivots[Timeframe.M15]:
            if (
                pivot.kind != pivot_kind
                or not (
                    location.zone.low - book.tick_size
                    <= pivot.price
                    <= location.zone.high + book.tick_size
                )
            ):
                continue
            paired_at = max(location.known_at, pivot.known_at)
            if pivot.known_at < paired_at and pivot_is_consumed(
                pivot,
                _frame_as_of(book, Timeframe.M15, paired_at),
                tick_size=book.tick_size,
            ):
                continue
            pair_count += 1
            start = max(
                1,
                int(frame.index.searchsorted(paired_at, side="left")),
            )
            for index in range(start, len(frame)):
                opened = frame.index[index]
                close_time = closes[index]
                if pivot.known_at > opened or location.known_at > opened:
                    continue
                if not _order_block_is_active(book, location, as_of=close_time):
                    break
                previous_close = float(frame.iloc[index - 1]["close"])
                row = frame.iloc[index]
                if location.side is Side.LONG:
                    qualifies = (
                        previous_close > pivot.price
                        and float(row["low"])
                        <= pivot.price - book.tick_size + 1e-12
                        and float(row["close"])
                        >= pivot.price + book.tick_size - 1e-12
                    )
                else:
                    qualifies = (
                        previous_close < pivot.price
                        and float(row["high"])
                        >= pivot.price + book.tick_size - 1e-12
                        and float(row["close"])
                        <= pivot.price - book.tick_size + 1e-12
                    )
                if not qualifies:
                    continue
                if _structure_location_side_is_allowed(
                    book,
                    location,
                    as_of=close_time,
                ):
                    event = LiquidityEvent(
                        event_id=(
                            f"{book.symbol}:5m:m15-location-sweep:"
                            f"{location.side.value}:{pivot.pivot_id}:"
                            f"{opened.isoformat()}"
                        ),
                        symbol=book.symbol,
                        timeframe=Timeframe.M5,
                        subtype=B1Subtype.SWEEP_RECLAIM,
                        side=location.side,
                        node_id=pivot.pivot_id,
                        node_price=pivot.price,
                        event_time=opened,
                        known_at=close_time,
                        event_extreme=(
                            float(row["low"])
                            if location.side is Side.LONG
                            else float(row["high"])
                        ),
                    )
                    raw.append(_LocationSweep(location, pivot, event))
                # The pair's first completed sweep consumes this opportunity,
                # even when the top-down direction does not admit an order.
                break

    # Nested M15 OBs can point to the same swept pivot.  They describe one
    # liquidity event; keep the newest, then narrowest active location.
    grouped: dict[tuple[str, pd.Timestamp, Side], list[_LocationSweep]] = {}
    for item in raw:
        grouped.setdefault(
            (item.pivot.pivot_id, item.event.event_time, item.event.side),
            [],
        ).append(item)
    selected = [
        min(
            items,
            key=lambda item: (
                -item.location.known_at.value,
                item.location.zone.width,
                item.location.ob_id,
            ),
        )
        for items in grouped.values()
    ]
    return (
        tuple(
            sorted(
                selected,
                key=lambda item: (item.event.known_at, item.event.event_id),
            )
        ),
        pair_count,
    )


def _external_destination_at_event(
    book: FeatureBook,
    event: LiquidityEvent,
) -> TargetCandidate | None:
    """Freeze the nearest external structure that already exists at the event."""

    target_pivot_kind = "high" if event.side is Side.LONG else "low"
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, event.known_at)
        pivots = [
            pivot
            for pivot in book.pivots[timeframe]
            if not (
                pivot.kind != target_pivot_kind
                or pivot.known_at > event.known_at
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
                or not _ahead(
                    event.side,
                    pivot.price,
                    event.node_price,
                    book.tick_size,
                )
            )
        ]
        if pivots:
            # The latest confirmed opposite boundary completes the M15/H1/H4
            # range story.  A nearer incidental zone is not allowed to replace
            # that pre-event external draw.
            pivot = max(
                pivots,
                key=lambda item: (
                    item.pivot_time,
                    -abs(item.price - event.node_price),
                    item.pivot_id,
                ),
            )
            return TargetCandidate(
                candidate_id=f"v05-event-destination:pivot:{pivot.pivot_id}",
                symbol=book.symbol,
                trade_side=event.side,
                kind="pivot",
                zone=PriceZone(pivot.price, pivot.price),
                known_at=event.known_at,
                source_id=pivot.pivot_id,
            )

    candidates: list[TargetCandidate] = []
    for timeframe in (Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, event.known_at)
        for block in book.order_blocks[timeframe]:
            if (
                block.side is event.side
                or block.known_at > event.known_at
                or zone_is_consumed(
                    block.zone,
                    frame,
                    travel_side=event.side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=block.known_at,
                )
            ):
                continue
            target = TargetCandidate(
                candidate_id=f"v05-event-destination:ob:{block.ob_id}",
                symbol=book.symbol,
                trade_side=event.side,
                kind="order_block",
                zone=block.zone,
                known_at=event.known_at,
                source_id=block.ob_id,
            )
            if _ahead(
                event.side,
                target.order_price,
                event.node_price,
                book.tick_size,
            ):
                candidates.append(target)

        for gap in book.fvgs[timeframe]:
            if (
                gap.side is event.side
                or gap.known_at > event.known_at
                or zone_is_consumed(
                    gap.zone,
                    frame,
                    travel_side=event.side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=gap.known_at,
                )
            ):
                continue
            target = TargetCandidate(
                candidate_id=f"v05-event-destination:fvg:{gap.fvg_id}",
                symbol=book.symbol,
                trade_side=event.side,
                kind="fvg",
                zone=gap.zone,
                known_at=event.known_at,
                source_id=gap.fvg_id,
            )
            if _ahead(
                event.side,
                target.order_price,
                event.node_price,
                book.tick_size,
            ):
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


def _owned_pivot(
    book: FeatureBook,
    *,
    side: Side,
    event: LiquidityEvent,
    displacement: FormationBar,
    preceding: tuple[FormationBar, ...],
) -> StrictPivot | None:
    if event.known_at > displacement.open_time:
        return None
    if side is Side.LONG and not displacement.bullish:
        return None
    if side is Side.SHORT and not displacement.bearish:
        return None

    pivot_kind = "high" if side is Side.LONG else "low"
    eligible = [
        pivot
        for pivot in book.pivots[Timeframe.M5]
        if pivot.kind == pivot_kind and pivot.known_at <= displacement.open_time
    ]
    if not eligible:
        return None
    pivot = max(
        eligible,
        key=lambda item: (item.pivot_time, item.known_at, item.pivot_id),
    )
    if side is Side.LONG:
        owns_break = (
            all(item.close <= pivot.price + 1e-12 for item in preceding)
            and displacement.close
            >= pivot.price + book.tick_size - 1e-12
        )
    else:
        owns_break = (
            all(item.close >= pivot.price - 1e-12 for item in preceding)
            and displacement.close
            <= pivot.price - book.tick_size + 1e-12
        )
    return pivot if owns_break else None


def _stop_for_delivery(
    book: FeatureBook,
    *,
    side: Side,
    event: LiquidityEvent,
    formation_bars: tuple[FormationBar, ...],
    formation_owner: DeliveryStopOwner,
) -> tuple[DeliveryStopOwner, float, float] | None:
    low = min(item.low for item in formation_bars)
    high = max(item.high for item in formation_bars)
    contains_event = low - 1e-12 <= event.event_extreme <= high + 1e-12
    owner: DeliveryStopOwner = formation_owner if contains_event else "m15_event"
    extreme = (
        (low if side is Side.LONG else high)
        if contains_event
        else event.event_extreme
    )
    initial_stop = (
        extreme - book.tick_size
        if side is Side.LONG
        else extreme + book.tick_size
    )
    if initial_stop <= 0:
        return None
    return owner, extreme, initial_stop


def _ob_candidate(
    book: FeatureBook,
    *,
    event: LiquidityEvent,
    location: OrderBlock,
    block: OrderBlock,
) -> _DeliveryCandidate | None:
    displacement = block.formation_bars[-1]
    if (
        block.timeframe is not Timeframe.M5
        or block.side is not event.side
        or block.known_at <= event.known_at
        or displacement.open_time < event.known_at
        or not _m5_sweep_episode_is_valid(
            book,
            event,
            until=block.known_at,
        )
    ):
        return None
    pivot = _owned_pivot(
        book,
        side=event.side,
        event=event,
        displacement=displacement,
        preceding=block.formation_bars[:-1],
    )
    if pivot is None:
        return None
    stop = _stop_for_delivery(
        book,
        side=event.side,
        event=event,
        formation_bars=block.formation_bars,
        formation_owner="m5_ob_formation",
    )
    if stop is None:
        return None
    zone, source = _refine_entry_zone(
        location,
        block.zone,
        base_source="ob_body",
        tick_size=book.tick_size,
    )
    owner, stop_extreme, initial_stop = stop
    return _DeliveryCandidate(
        known_at=block.known_at,
        kind="ob",
        delivery_root_id=(
            f"{book.symbol}:5m:delivery:{displacement.open_time.isoformat()}"
        ),
        pivot=pivot,
        order_block=block,
        fvg=None,
        zone=zone,
        entry_zone_source=source,
        stop_owner=owner,
        stop_extreme=stop_extreme,
        initial_stop=initial_stop,
        impulse_extreme=(
            max(item.high for item in block.formation_bars)
            if event.side is Side.LONG
            else min(item.low for item in block.formation_bars)
        ),
    )


def _fvg_candidate(
    book: FeatureBook,
    *,
    event: LiquidityEvent,
    location: OrderBlock,
    gap: FairValueGap,
) -> _DeliveryCandidate | None:
    a, displacement, _ = gap.formation_bars
    if (
        gap.timeframe is not Timeframe.M5
        or gap.side is not event.side
        or gap.known_at <= event.known_at
        or displacement.open_time < event.known_at
        or not _m5_sweep_episode_is_valid(
            book,
            event,
            until=gap.known_at,
        )
    ):
        return None
    pivot = _owned_pivot(
        book,
        side=event.side,
        event=event,
        displacement=displacement,
        preceding=(a,),
    )
    if pivot is None:
        return None
    stop = _stop_for_delivery(
        book,
        side=event.side,
        event=event,
        formation_bars=gap.formation_bars,
        formation_owner="m5_fvg_formation",
    )
    if stop is None:
        return None
    zone, source = _refine_entry_zone(
        location,
        gap.zone,
        base_source="fvg_wick_gap",
        tick_size=book.tick_size,
    )
    owner, stop_extreme, initial_stop = stop
    return _DeliveryCandidate(
        known_at=gap.known_at,
        kind="fvg",
        delivery_root_id=(
            f"{book.symbol}:5m:delivery:{displacement.open_time.isoformat()}"
        ),
        pivot=pivot,
        order_block=None,
        fvg=gap,
        zone=zone,
        entry_zone_source=source,
        stop_owner=owner,
        stop_extreme=stop_extreme,
        initial_stop=initial_stop,
        impulse_extreme=(
            max(item.high for item in gap.formation_bars)
            if event.side is Side.LONG
            else min(item.low for item in gap.formation_bars)
        ),
    )


def _candidate_key(item: _DeliveryCandidate) -> tuple[object, ...]:
    root_time = item.delivery_root_id.rsplit(":delivery:", 1)[-1]
    return (
        item.known_at,
        root_time,
        0 if item.entry_zone_source == "m15_m5_intersection" else 1,
        item.zone.width,
        item.order_block.ob_id
        if item.order_block is not None
        else item.fvg.fvg_id,
    )


def build_m15_m5_liquidity_delivery_result(
    book: FeatureBook,
) -> V05BuildResult:
    """Build M15 location -> M5 sweep -> owned delivery -> first revisit."""

    location_sweeps, pair_count = _detect_location_sweeps(book)
    raw_authorities: list[LiquidityDeliveryAuthority] = []
    ob_candidate_count = 0
    fvg_candidate_count = 0
    episodes_without_delivery = 0
    targets_missing = 0
    targets_used = 0

    m5_blocks = book.order_blocks[Timeframe.M5]
    m5_fvgs = book.fvgs[Timeframe.M5]
    for item in location_sweeps:
        event = item.event
        destination = _external_destination_at_event(book, event)
        if destination is None:
            targets_missing += 1
            continue

        candidates: list[_DeliveryCandidate] = []
        for block in m5_blocks:
            candidate = _ob_candidate(
                book,
                event=event,
                location=item.location,
                block=block,
            )
            if candidate is not None:
                ob_candidate_count += 1
                candidates.append(candidate)
        for gap in m5_fvgs:
            candidate = _fvg_candidate(
                book,
                event=event,
                location=item.location,
                gap=gap,
            )
            if candidate is not None:
                fvg_candidate_count += 1
                candidates.append(candidate)
        if not candidates:
            episodes_without_delivery += 1
            continue

        delivery = min(candidates, key=_candidate_key)
        if not _order_block_is_active(
            book,
            item.location,
            as_of=delivery.known_at,
        ):
            episodes_without_delivery += 1
            continue
        if _target_touched(
            book,
            destination,
            after=event.known_at,
            through=delivery.known_at,
        ):
            targets_used += 1
            continue

        raw_authorities.append(
            LiquidityDeliveryAuthority(
                authority_id=(
                    f"v05-liquidity-delivery:{item.location.ob_id}|"
                    f"{event.event_id}|{delivery.delivery_root_id}|"
                    f"{delivery.kind}"
                ),
                symbol=book.symbol,
                side=event.side,
                location_ob=item.location,
                liquidity_event=event,
                delivery_kind=delivery.kind,
                delivery_root_id=delivery.delivery_root_id,
                displacement_pivot=delivery.pivot,
                delivery_ob=delivery.order_block,
                delivery_fvg=delivery.fvg,
                zone=delivery.zone,
                entry_zone_source=delivery.entry_zone_source,
                known_at=delivery.known_at,
                stop_owner=delivery.stop_owner,
                stop_extreme=delivery.stop_extreme,
                initial_stop=delivery.initial_stop,
                impulse_extreme=delivery.impulse_extreme,
                destination=destination,
            )
        )

    # Several swept nodes can explain one completed delivery.  At an intent
    # timestamp and side this is one orderable scene, not several orders.
    grouped: dict[
        tuple[pd.Timestamp, Side], list[LiquidityDeliveryAuthority]
    ] = {}
    for authority in raw_authorities:
        grouped.setdefault((authority.known_at, authority.side), []).append(authority)
    selected = [
        min(
            items,
            key=lambda authority: (
                -authority.liquidity_event.known_at.value,
                0
                if authority.entry_zone_source == "m15_m5_intersection"
                else 1,
                authority.zone.width,
                -authority.location_ob.known_at.value,
                authority.authority_id,
            ),
        )
        for items in grouped.values()
    ]
    authorities = tuple(
        sorted(selected, key=lambda item: (item.known_at, item.authority_id))
    )
    diagnostics = V05BuildDiagnostics(
        m15_locations=len(book.order_blocks[Timeframe.M15]),
        location_pivot_pairs=pair_count,
        location_sweep_events=len(location_sweeps),
        ob_delivery_candidates=ob_candidate_count,
        fvg_delivery_candidates=fvg_candidate_count,
        episodes_without_delivery=episodes_without_delivery,
        targets_missing_at_event=targets_missing,
        targets_used_before_delivery=targets_used,
        duplicate_scenes_suppressed=len(raw_authorities) - len(authorities),
        authorities=len(authorities),
    )
    return V05BuildResult(authorities, diagnostics)


def build_m15_m5_liquidity_delivery_authorities(
    book: FeatureBook,
) -> tuple[LiquidityDeliveryAuthority, ...]:
    return build_m15_m5_liquidity_delivery_result(book).authorities


__all__ = [
    "V05BuildDiagnostics",
    "V05BuildResult",
    "build_m15_m5_liquidity_delivery_authorities",
    "build_m15_m5_liquidity_delivery_result",
]
