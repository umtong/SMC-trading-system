from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Sequence

import pandas as pd

from .domain import (
    B1Subtype,
    ConfirmationModel,
    ConfluenceAuthority,
    FairValueGap,
    FormationBar,
    LiquidityEvent,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
    TriggerAuthority,
)
from .features import TIMEFRAME_DELTA, intersect_zones


TargetRejectionReason = Literal["no_target_ahead", "target_space_conflict"]
InitialStopOwner = Literal["execution_ob", "liquidity_event"]


@dataclass(frozen=True, slots=True)
class InitialStopSelection:
    """The fixed pre-entry stop selected for a 15m-event -> 5m-OB scene."""

    owner: InitialStopOwner
    stop_extreme: float
    initial_stop: float


def select_scene_initial_stop(
    execution_block: OrderBlock,
    *,
    event_extreme: float,
    side: Side,
    tick_size: float,
) -> InitialStopSelection:
    """Select the scene invalidation boundary before an order is created.

    The execution OB owns the stop only when its complete formation wick range
    contains the 15m liquidity-event extreme. Otherwise the event extreme is
    the structural invalidation boundary. The returned stop is always one tick
    beyond the selected boundary and is not a trailing-stop rule.
    """

    if execution_block.side is not side:
        raise ValueError("execution block side disagrees with scene side")
    event_boundary = _positive(event_extreme, name="event_extreme")
    tick = _tick(tick_size)
    formation_low = min(bar.low for bar in execution_block.formation_bars)
    formation_high = max(bar.high for bar in execution_block.formation_bars)
    block_owns_event = formation_low - 1e-12 <= event_boundary <= formation_high + 1e-12
    owner: InitialStopOwner = "execution_ob" if block_owns_event else "liquidity_event"
    stop_extreme = execution_block.stop_extreme if block_owns_event else event_boundary
    initial_stop = stop_extreme - tick if side is Side.LONG else stop_extreme + tick
    if initial_stop <= 0:
        raise ValueError("tick_size puts the initial stop at a non-positive price")
    return InitialStopSelection(owner, stop_extreme, initial_stop)


def _tick(value: float) -> float:
    tick = float(value)
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("tick_size must be finite and positive")
    return tick


def _positive(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _non_negative(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _levels_from_block(
    block: OrderBlock, *, side: Side, tick_size: float
) -> tuple[float, float, float]:
    """Use the execution block, not the wider A1 location, for risk geometry."""

    tick = _tick(tick_size)
    bars = block.formation_bars
    if side is Side.LONG:
        stop_extreme = min(bar.low for bar in bars)
        initial_stop = stop_extreme - tick
        impulse_extreme = max(bar.high for bar in bars)
    else:
        stop_extreme = max(bar.high for bar in bars)
        initial_stop = stop_extreme + tick
        impulse_extreme = min(bar.low for bar in bars)
    if initial_stop <= 0:
        raise ValueError("tick_size puts the initial stop at a non-positive price")
    return stop_extreme, initial_stop, impulse_extreme


def _validate_execution_bar(bar: FormationBar, timeframe: Timeframe) -> None:
    if timeframe not in {Timeframe.M5, Timeframe.M15}:
        raise ValueError("B1 confirmation timeframe must be 5m or 15m")
    if bar.close_time - bar.open_time != TIMEFRAME_DELTA[timeframe]:
        raise ValueError("bar duration does not match the confirmation timeframe")


def _validate_node_and_adjacent_bars(
    pivot: StrictPivot,
    *,
    timeframe: Timeframe,
    previous_bar: FormationBar,
    event_bar: FormationBar,
) -> None:
    if pivot.timeframe is not Timeframe.H1:
        raise ValueError("optional liquidity-event node must be a strict 1h pivot")
    _validate_execution_bar(previous_bar, timeframe)
    _validate_execution_bar(event_bar, timeframe)
    if previous_bar.close_time != event_bar.open_time:
        raise ValueError("previous and event bars must be contiguous")
    if pivot.known_at > event_bar.open_time:
        raise ValueError("pivot was not known when the event bar opened")


def detect_b1_reversal_event(
    pivot: StrictPivot,
    *,
    timeframe: Timeframe,
    previous_bar: FormationBar,
    event_bar: FormationBar,
    tick_size: float,
) -> LiquidityEvent | None:
    """Detect a completed lower-timeframe sweep/reclaim liquidity event."""

    _validate_node_and_adjacent_bars(
        pivot,
        timeframe=timeframe,
        previous_bar=previous_bar,
        event_bar=event_bar,
    )
    tick = _tick(tick_size)
    if pivot.kind == "low":
        side = Side.LONG
        qualifies = (
            previous_bar.close > pivot.price
            and event_bar.low <= pivot.price - tick + 1e-12
            and event_bar.close >= pivot.price + tick - 1e-12
        )
    else:
        side = Side.SHORT
        qualifies = (
            previous_bar.close < pivot.price
            and event_bar.high >= pivot.price + tick - 1e-12
            and event_bar.close <= pivot.price - tick + 1e-12
        )
    if not qualifies:
        return None
    return LiquidityEvent(
        event_id=(
            f"{pivot.symbol}:{timeframe.value}:sweep_reclaim:{side.value}:"
            f"{pivot.pivot_id}:{event_bar.open_time.isoformat()}"
        ),
        symbol=pivot.symbol,
        timeframe=timeframe,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=side,
        node_id=pivot.pivot_id,
        node_price=pivot.price,
        event_time=event_bar.open_time,
        known_at=event_bar.close_time,
        event_extreme=event_bar.low if side is Side.LONG else event_bar.high,
    )


def detect_b1_continuation_event(
    pivot: StrictPivot,
    *,
    timeframe: Timeframe,
    previous_bar: FormationBar,
    break_bar: FormationBar,
    post_break_bars: Sequence[FormationBar],
    tick_size: float,
) -> LiquidityEvent | None:
    """Detect a completed lower-timeframe break/retest liquidity event."""

    _validate_node_and_adjacent_bars(
        pivot,
        timeframe=timeframe,
        previous_bar=previous_bar,
        event_bar=break_bar,
    )
    tick = _tick(tick_size)
    if pivot.kind == "high":
        side = Side.LONG
        broken = (
            previous_bar.close <= pivot.price + 1e-12
            and break_bar.close >= pivot.price + tick - 1e-12
        )
    else:
        side = Side.SHORT
        broken = (
            previous_bar.close >= pivot.price - 1e-12
            and break_bar.close <= pivot.price - tick + 1e-12
        )
    if not broken:
        return None

    expected_open = break_bar.close_time
    for bar in post_break_bars:
        _validate_execution_bar(bar, timeframe)
        if bar.open_time != expected_open:
            raise ValueError("post-break bars must be complete and contiguous")
        expected_open = bar.close_time
        hold_failed = (
            bar.close <= pivot.price - tick + 1e-12
            if side is Side.LONG
            else bar.close >= pivot.price + tick - 1e-12
        )
        if hold_failed:
            return None
        touched = (
            bar.low <= pivot.price + 1e-12
            if side is Side.LONG
            else bar.high >= pivot.price - 1e-12
        )
        if not touched:
            continue
        accepted = (
            bar.close >= pivot.price + tick - 1e-12
            if side is Side.LONG
            else bar.close <= pivot.price - tick + 1e-12
        )
        if not accepted:
            return None
        return LiquidityEvent(
            event_id=(
                f"{pivot.symbol}:{timeframe.value}:break_retest:{side.value}:"
                f"{pivot.pivot_id}:{break_bar.open_time.isoformat()}:"
                f"{bar.open_time.isoformat()}"
            ),
            symbol=pivot.symbol,
            timeframe=timeframe,
            subtype=B1Subtype.BREAK_RETEST,
            side=side,
            node_id=pivot.pivot_id,
            node_price=pivot.price,
            event_time=bar.open_time,
            known_at=bar.close_time,
            event_extreme=bar.low if side is Side.LONG else bar.high,
        )
    return None


def detect_b1_liquidity_events(
    pivots: Sequence[StrictPivot],
    *,
    timeframe: Timeframe,
    bars: Sequence[FormationBar],
    tick_size: float,
) -> tuple[LiquidityEvent, ...]:
    """Detect the first reversal and continuation event for each 1H node.

    The returned events are completed observations on the execution timeframe.
    They do not create orders by themselves.  The first same-side B1 order
    block formed during the still-valid reaction episode confirms the event.
    """

    if timeframe not in {Timeframe.M5, Timeframe.M15}:
        raise ValueError("B1 liquidity events require 5m or 15m bars")
    _tick(tick_size)
    ordered_bars = tuple(bars)
    if tuple(sorted(ordered_bars, key=lambda bar: bar.open_time)) != ordered_bars:
        raise ValueError("liquidity-event bars must be chronological")
    for previous, current in zip(ordered_bars, ordered_bars[1:]):
        _validate_execution_bar(previous, timeframe)
        _validate_execution_bar(current, timeframe)
        if previous.close_time != current.open_time:
            raise ValueError("liquidity-event bars must be complete and contiguous")

    events: dict[str, LiquidityEvent] = {}
    tick = _tick(tick_size)
    for pivot in pivots:
        if pivot.timeframe is not Timeframe.H1:
            raise ValueError("B1 liquidity-event nodes must be strict 1h pivots")
        reversal_found = False
        continuation_found = False
        break_index: int | None = None
        for index in range(1, len(ordered_bars)):
            previous = ordered_bars[index - 1]
            current = ordered_bars[index]
            if pivot.known_at > current.open_time:
                continue

            if not reversal_found:
                reversal = detect_b1_reversal_event(
                    pivot,
                    timeframe=timeframe,
                    previous_bar=previous,
                    event_bar=current,
                    tick_size=tick,
                )
                if reversal is not None:
                    events[reversal.event_id] = reversal
                    reversal_found = True

            if continuation_found:
                continue
            if break_index is None:
                broken = (
                    previous.close <= pivot.price + 1e-12
                    and current.close >= pivot.price + tick - 1e-12
                    if pivot.kind == "high"
                    else previous.close >= pivot.price - 1e-12
                    and current.close <= pivot.price - tick + 1e-12
                )
                if broken:
                    break_index = index
                continue

            hold_failed = (
                current.close <= pivot.price - tick + 1e-12
                if pivot.kind == "high"
                else current.close >= pivot.price + tick - 1e-12
            )
            if hold_failed:
                break_index = None
                continue
            touched = (
                current.low <= pivot.price + 1e-12
                if pivot.kind == "high"
                else current.high >= pivot.price - 1e-12
            )
            if not touched:
                continue
            accepted = (
                current.close >= pivot.price + tick - 1e-12
                if pivot.kind == "high"
                else current.close <= pivot.price - tick + 1e-12
            )
            if not accepted:
                break_index = None
                continue
            side = Side.LONG if pivot.kind == "high" else Side.SHORT
            break_bar = ordered_bars[break_index]
            continuation = LiquidityEvent(
                event_id=(
                    f"{pivot.symbol}:{timeframe.value}:break_retest:{side.value}:"
                    f"{pivot.pivot_id}:{break_bar.open_time.isoformat()}:"
                    f"{current.open_time.isoformat()}"
                ),
                symbol=pivot.symbol,
                timeframe=timeframe,
                subtype=B1Subtype.BREAK_RETEST,
                side=side,
                node_id=pivot.pivot_id,
                node_price=pivot.price,
                event_time=current.open_time,
                known_at=current.close_time,
                event_extreme=current.low if side is Side.LONG else current.high,
            )
            events[continuation.event_id] = continuation
            continuation_found = True
            break_index = None

    return tuple(
        sorted(events.values(), key=lambda event: (event.known_at, event.event_id))
    )


def build_b1_confirmation(
    block: OrderBlock, *, event: LiquidityEvent
) -> TriggerAuthority:
    """Combine a completed liquidity event and same-side M5/M15 OB as B1."""

    if block.timeframe not in {Timeframe.M5, Timeframe.M15}:
        raise ValueError("B1 confirmation must be a 5m or 15m order block")
    if (
        event.symbol != block.symbol
        or event.timeframe is not block.timeframe
        or event.side is not block.side
    ):
        raise ValueError("liquidity event does not belong to the B1 block")
    if event.known_at > block.known_at:
        raise ValueError("B1 block cannot be known before its liquidity event")
    return TriggerAuthority(
        authority_id=f"b1-confirmation:{block.ob_id}",
        symbol=block.symbol,
        subtype=event.subtype,
        side=block.side,
        timeframes=(block.timeframe,),
        order_blocks=(block,),
        zone=block.zone,
        known_at=block.known_at,
        stop_extreme=block.stop_extreme,
        initial_stop=block.initial_stop,
        impulse_extreme=block.impulse_extreme,
        liquidity_event_id=event.event_id,
        liquidity_node_id=event.node_id,
        liquidity_node_price=event.node_price,
        liquidity_event_extreme=event.event_extreme,
    )


def match_m5_mss_displacement_pivot(
    block: OrderBlock,
    *,
    event: LiquidityEvent,
    pivots: Sequence[StrictPivot],
    tick_size: float,
) -> StrictPivot | None:
    """Return the latest confirmed 5m swing broken by the OB's delivery bar.

    The break is owned by the body-engulf formation itself: every earlier
    formation close remains on the pre-break side and the final, directional
    formation bar closes at least one tick through the latest swing that was
    already confirmed when that final bar opened.
    """

    tick = _tick(tick_size)
    if block.timeframe is not Timeframe.M5:
        raise ValueError("MSS displacement owner must be a 5m order block")
    if event.timeframe is not Timeframe.M15:
        raise ValueError("MSS displacement must follow a completed 15m liquidity event")
    if event.symbol != block.symbol or event.side is not block.side:
        raise ValueError("15m liquidity event and 5m order block must share symbol and side")

    displacement = block.formation_bars[-1]
    if event.known_at > displacement.open_time:
        return None
    pivot_kind = "high" if block.side is Side.LONG else "low"
    eligible = tuple(
        pivot
        for pivot in pivots
        if pivot.symbol == block.symbol
        and pivot.timeframe is Timeframe.M5
        and pivot.kind == pivot_kind
        and pivot.known_at <= displacement.open_time
    )
    if not eligible:
        return None
    latest = max(
        eligible,
        key=lambda pivot: (pivot.known_at, pivot.pivot_time, pivot.pivot_id),
    )
    preceding = block.formation_bars[:-1]
    if block.side is Side.LONG:
        qualifies = (
            displacement.bullish
            and all(bar.close <= latest.price + 1e-12 for bar in preceding)
            and displacement.close >= latest.price + tick - 1e-12
        )
    else:
        qualifies = (
            displacement.bearish
            and all(bar.close >= latest.price - 1e-12 for bar in preceding)
            and displacement.close <= latest.price - tick + 1e-12
        )
    return latest if qualifies else None


def build_m15_liquidity_m5_delivery_confirmation(
    block: OrderBlock,
    *,
    event: LiquidityEvent,
    pivots: Sequence[StrictPivot],
    tick_size: float,
) -> TriggerAuthority | None:
    """Build the `m15_liquidity_m5_mss_ob.v1` causal delivery confirmation."""

    mss_pivot = match_m5_mss_displacement_pivot(
        block,
        event=event,
        pivots=pivots,
        tick_size=tick_size,
    )
    if mss_pivot is None:
        return None
    return TriggerAuthority(
        authority_id=(
            f"m15-m5-delivery:{event.event_id}|{mss_pivot.pivot_id}|{block.ob_id}"
        ),
        symbol=block.symbol,
        subtype=event.subtype,
        side=block.side,
        timeframes=(Timeframe.M5,),
        order_blocks=(block,),
        zone=block.zone,
        known_at=block.known_at,
        stop_extreme=block.stop_extreme,
        initial_stop=block.initial_stop,
        impulse_extreme=block.impulse_extreme,
        liquidity_event_id=event.event_id,
        liquidity_node_id=event.node_id,
        liquidity_node_price=event.node_price,
        confirmation_model=ConfirmationModel.M15_LIQUIDITY_M5_MSS_OB_V1,
        liquidity_event_timeframe=Timeframe.M15,
        displacement_pivot_id=mss_pivot.pivot_id,
        displacement_pivot_price=mss_pivot.price,
        liquidity_event_extreme=event.event_extreme,
    )


def compose_a1_b1_confluence(
    location: OrderBlock | StrictPivot,
    confirmation: TriggerAuthority,
    *,
    tick_size: float,
) -> ConfluenceAuthority | None:
    """Join an existing A1 location with a later event-confirmed B1 OB.

    A literal body overlap refines the entry zone when present.  It is not a
    prerequisite: the B1 body remains the executable zone in a sequential
    location -> liquidity event -> OB reaction.
    """

    tick = _tick(tick_size)
    allowed_ob_pairs = (
        (Timeframe.H1, Timeframe.M15),
        (Timeframe.H1, Timeframe.M5),
        (Timeframe.M15, Timeframe.M5),
    )
    if (
        location.symbol != confirmation.symbol
        or confirmation.known_at <= location.known_at
    ):
        return None

    pair = (location.timeframe, confirmation.timeframes[0])
    overlap: PriceZone | None = None
    if isinstance(location, OrderBlock):
        if location.side is not confirmation.side or pair not in allowed_ob_pairs:
            return None
        if not (
            location.zone.low - tick
            <= confirmation.liquidity_node_price
            <= location.zone.high + tick
        ):
            return None
        overlap = intersect_zones(
            (location.zone, confirmation.zone), minimum_width=tick
        )
        location_id = location.ob_id
    else:
        expected_pivot_kind = (
            "low"
            if confirmation.subtype is B1Subtype.SWEEP_RECLAIM
            and confirmation.side is Side.LONG
            else "high"
            if confirmation.subtype is B1Subtype.SWEEP_RECLAIM
            else "high"
            if confirmation.side is Side.LONG
            else "low"
        )
        if (
            location.timeframe is not Timeframe.H1
            or pair
            not in {
                (Timeframe.H1, Timeframe.M15),
                (Timeframe.H1, Timeframe.M5),
            }
            or location.pivot_id != confirmation.liquidity_node_id
            or location.kind != expected_pivot_kind
        ):
            return None
        location_id = location.pivot_id

    zone = confirmation.zone if overlap is None else overlap
    execution_block = confirmation.order_blocks[0]
    _block_stop, _block_initial_stop, impulse_extreme = _levels_from_block(
        execution_block, side=confirmation.side, tick_size=tick
    )
    stop_selection = select_scene_initial_stop(
        execution_block,
        event_extreme=confirmation.liquidity_event_extreme,
        side=confirmation.side,
        tick_size=tick,
    )
    stop_extreme = stop_selection.stop_extreme
    initial_stop = stop_selection.initial_stop
    return ConfluenceAuthority(
        authority_id=f"confluence:{location_id}|{confirmation.authority_id}",
        symbol=location.symbol,
        side=confirmation.side,
        location=location,
        confirmation=confirmation,
        zone=zone,
        known_at=confirmation.known_at,
        stop_extreme=stop_extreme,
        initial_stop=initial_stop,
        impulse_extreme=impulse_extreme,
    )


def select_preferred_confluence(
    authorities: Sequence[ConfluenceAuthority], *, symbol: str, side: Side
) -> ConfluenceAuthority | None:
    """Prefer M15+M5 precision, then M15 confirmation, then direct M5."""

    eligible = [
        authority
        for authority in authorities
        if authority.symbol == symbol and authority.side is side
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda authority: (
            (
                0
                if authority.location.timeframe is Timeframe.M15
                and authority.confirmation.timeframes == (Timeframe.M5,)
                else 1
                if authority.confirmation.timeframes == (Timeframe.M15,)
                else 2
            ),
            0 if authority.has_literal_body_overlap else 1,
            authority.zone.width,
            -authority.known_at.value,
            authority.authority_id,
        ),
    )


def confluence_entry_price(authority: ConfluenceAuthority) -> float:
    """Proximal price for the first revisit after B1 completion."""

    return authority.zone.high if authority.side is Side.LONG else authority.zone.low


def build_target_candidates(
    authority: ConfluenceAuthority,
    *,
    pivots: Sequence[StrictPivot] = (),
    order_blocks: Sequence[OrderBlock] = (),
    fvgs: Sequence[FairValueGap] = (),
    as_of: pd.Timestamp | None = None,
    excluded_source_ids: frozenset[str] = frozenset(),
) -> tuple[TargetCandidate, ...]:
    """Create the structural target universe known at confluence completion."""

    cutoff = authority.known_at if as_of is None else _utc(as_of, name="as_of")
    owners = frozenset(
        timeframe
        for timeframe in (
            authority.location.timeframe,
            authority.confirmation.liquidity_event_timeframe,
            *authority.confirmation.timeframes,
        )
        if timeframe in {Timeframe.M5, Timeframe.M15}
    )
    side = authority.side
    symbol = authority.symbol
    # The execution OB's own impulse extreme is not an independent destination.
    # A pivot, opposing OB, or opposing FVG at the same price retains its own
    # structural target authority.
    output: list[TargetCandidate] = []

    target_pivot_kind = "high" if side is Side.LONG else "low"
    allowed_pivot_timeframes = owners | {Timeframe.H1, Timeframe.H4}
    for pivot in pivots:
        if (
            pivot.symbol != symbol
            or pivot.kind != target_pivot_kind
            or pivot.timeframe not in allowed_pivot_timeframes
            or pivot.known_at > cutoff
            or pivot.pivot_id in excluded_source_ids
        ):
            continue
        output.append(
            TargetCandidate(
                candidate_id=f"{pivot.pivot_id}:target",
                symbol=symbol,
                trade_side=side,
                kind="pivot",
                zone=PriceZone(pivot.price, pivot.price),
                known_at=pivot.known_at,
                source_id=pivot.pivot_id,
            )
        )

    for block in order_blocks:
        if (
            block.symbol != symbol
            or block.side is side
            or block.timeframe not in {Timeframe.M15, Timeframe.H1, Timeframe.H4}
            or block.known_at > cutoff
            or block.ob_id in excluded_source_ids
        ):
            continue
        output.append(
            TargetCandidate(
                candidate_id=f"{block.ob_id}:target",
                symbol=symbol,
                trade_side=side,
                kind="order_block",
                zone=block.zone,
                known_at=block.known_at,
                source_id=block.ob_id,
            )
        )
    for fvg in fvgs:
        if (
            fvg.symbol != symbol
            or fvg.side is side
            or fvg.timeframe not in {Timeframe.M15, Timeframe.H1, Timeframe.H4}
            or fvg.known_at > cutoff
            or fvg.fvg_id in excluded_source_ids
        ):
            continue
        output.append(
            TargetCandidate(
                candidate_id=f"{fvg.fvg_id}:target",
                symbol=symbol,
                trade_side=side,
                kind="fvg",
                zone=fvg.zone,
                known_at=fvg.known_at,
                source_id=fvg.fvg_id,
            )
        )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.zone.low,
                item.zone.high,
                item.known_at,
                item.candidate_id,
            ),
        )
    )


def merge_target_candidates(
    candidates: Sequence[TargetCandidate],
) -> tuple[TargetCandidate, ...]:
    if not candidates:
        return ()
    first = candidates[0]
    if any(
        candidate.symbol != first.symbol or candidate.trade_side is not first.trade_side
        for candidate in candidates
    ):
        raise ValueError("target candidates must share symbol and trade side")
    ordered = sorted(
        candidates, key=lambda item: (item.zone.low, item.zone.high, item.candidate_id)
    )
    groups: list[list[TargetCandidate]] = [[ordered[0]]]
    group_high = ordered[0].zone.high
    for candidate in ordered[1:]:
        if candidate.zone.low <= group_high + 1e-12:
            groups[-1].append(candidate)
            group_high = max(group_high, candidate.zone.high)
        else:
            groups.append([candidate])
            group_high = candidate.zone.high

    merged: list[TargetCandidate] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        representative = (
            min(group, key=lambda item: (item.zone.low, item.candidate_id))
            if first.trade_side is Side.LONG
            else max(group, key=lambda item: (item.zone.high, item.candidate_id))
        )
        merged.append(
            TargetCandidate(
                candidate_id="merged:" + "|".join(
                    sorted(item.candidate_id for item in group)
                ),
                symbol=first.symbol,
                trade_side=first.trade_side,
                kind=representative.kind,
                zone=PriceZone(
                    min(item.zone.low for item in group),
                    max(item.zone.high for item in group),
                ),
                known_at=max(item.known_at for item in group),
                source_id="|".join(sorted({item.source_id for item in group})),
            )
        )
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class SimpleExecutionCosts:
    entry_fee_rate: float = 0.0
    exit_fee_rate: float = 0.0
    exit_slippage_bps: float = 0.0
    contract_value_adjustment: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_fee_rate", _non_negative(self.entry_fee_rate, name="entry_fee_rate"))
        object.__setattr__(self, "exit_fee_rate", _non_negative(self.exit_fee_rate, name="exit_fee_rate"))
        object.__setattr__(self, "exit_slippage_bps", _non_negative(self.exit_slippage_bps, name="exit_slippage_bps"))
        object.__setattr__(self, "contract_value_adjustment", _positive(self.contract_value_adjustment, name="contract_value_adjustment"))


def estimated_net_pnl(
    *,
    side: Side,
    entry_price: float,
    exit_price: float,
    quantity: float,
    costs: SimpleExecutionCosts,
) -> float:
    entry = _positive(entry_price, name="entry_price")
    exit_value = _positive(exit_price, name="exit_price")
    size = _positive(quantity, name="quantity")
    direction = 1.0 if side is Side.LONG else -1.0
    gross = direction * (exit_value - entry) * size * costs.contract_value_adjustment
    entry_fee = entry * size * costs.entry_fee_rate
    exit_fee = exit_value * size * costs.exit_fee_rate
    slippage = exit_value * size * costs.exit_slippage_bps / 10_000
    return gross - entry_fee - exit_fee - slippage


@dataclass(frozen=True, slots=True)
class TargetSelection:
    target: TargetCandidate | None
    estimated_net_pnl: float | None
    rejection_reason: TargetRejectionReason | None

    def __post_init__(self) -> None:
        if self.target is None:
            if self.rejection_reason is None:
                raise ValueError("a rejected target selection requires a reason")
        elif self.rejection_reason is not None or self.estimated_net_pnl is None:
            raise ValueError("an accepted target selection cannot have a rejection reason")


def select_initial_target(
    candidates: Sequence[TargetCandidate],
    *,
    side: Side,
    entry_price: float,
    tick_size: float,
    costs: SimpleExecutionCosts = SimpleExecutionCosts(),
    quantity: float = 1.0,
) -> TargetSelection:
    entry = _positive(entry_price, name="entry_price")
    tick = _tick(tick_size)
    size = _positive(quantity, name="quantity")
    if any(candidate.trade_side is not side for candidate in candidates):
        raise ValueError("candidate side disagrees with target selection side")
    merged = merge_target_candidates(candidates)
    # A merged opposing zone can straddle the entry while containing otherwise
    # valid point targets.  It is the nearest path obstacle and must reject the
    # scene; dropping it first would incorrectly jump to a much farther target.
    in_path = [
        candidate
        for candidate in merged
        if (
            candidate.zone.high >= entry + tick - 1e-12
            if side is Side.LONG
            else candidate.zone.low <= entry - tick + 1e-12
        )
    ]
    if not in_path:
        return TargetSelection(None, None, "no_target_ahead")
    nearest = (
        min(
            in_path,
            key=lambda item: (
                max(0.0, item.zone.low - entry),
                item.order_price,
                item.candidate_id,
            ),
        )
        if side is Side.LONG
        else min(
            in_path,
            key=lambda item: (
                max(0.0, entry - item.zone.high),
                -item.order_price,
                item.candidate_id,
            ),
        )
    )
    if (
        nearest.order_price < entry + tick - 1e-12
        if side is Side.LONG
        else nearest.order_price > entry - tick + 1e-12
    ):
        return TargetSelection(None, None, "target_space_conflict")
    net = estimated_net_pnl(
        side=side,
        entry_price=entry,
        exit_price=nearest.order_price,
        quantity=size,
        costs=costs,
    )
    if net <= 0:
        return TargetSelection(None, net, "target_space_conflict")
    return TargetSelection(nearest, net, None)


__all__ = [
    "InitialStopSelection",
    "SimpleExecutionCosts",
    "TargetSelection",
    "build_b1_confirmation",
    "build_m15_liquidity_m5_delivery_confirmation",
    "build_target_candidates",
    "compose_a1_b1_confluence",
    "confluence_entry_price",
    "detect_b1_continuation_event",
    "detect_b1_liquidity_events",
    "detect_b1_reversal_event",
    "estimated_net_pnl",
    "match_m5_mss_displacement_pivot",
    "merge_target_candidates",
    "select_initial_target",
    "select_preferred_confluence",
    "select_scene_initial_stop",
]


