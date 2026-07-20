from __future__ import annotations

from dataclasses import dataclass
import math
import statistics

import pandas as pd

from .domain import (
    B1Subtype,
    LiquidityDeliveryAuthority,
    LiquidityEvent,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .execution import CostConfig
from .features import TIMEFRAME_DELTA, pivot_is_consumed
from .pipeline import FeatureBook, _frame_as_of, _order_block_is_active
from .v04 import (
    _m5_sweep_episode_is_valid,
    _structure_location_side_is_allowed,
    _target_touched,
)
from .v05 import _candidate_key, _fvg_candidate, _ob_candidate


@dataclass(frozen=True, slots=True)
class V08IntradayPolicy:
    """M15 location -> internal M5 liquidity -> owned M5 delivery policy.

    The M5 pivot is internal inducement inside a pre-existing M15 order block.
    The target is not an arbitrary opposing FVG/OB; it is a pre-event,
    unconsumed M15/H1/H4 pivot in the delivery direction.
    """

    maximum_delivery_delay_bars: int = 12
    displacement_lookback_bars: int = 20
    minimum_displacement_history_bars: int = 8
    minimum_displacement_range_multiple: float = 1.10
    minimum_displacement_body_fraction: float = 0.50
    minimum_target_r: float = 0.65
    risk_fraction: float = 0.03
    maximum_notional_to_equity: float = 8.0

    def __post_init__(self) -> None:
        if self.maximum_delivery_delay_bars <= 0:
            raise ValueError("maximum_delivery_delay_bars must be positive")
        if self.displacement_lookback_bars <= 0:
            raise ValueError("displacement_lookback_bars must be positive")
        if not 1 <= self.minimum_displacement_history_bars <= self.displacement_lookback_bars:
            raise ValueError("minimum displacement history is invalid")
        for name in (
            "minimum_displacement_range_multiple",
            "minimum_displacement_body_fraction",
            "minimum_target_r",
            "risk_fraction",
            "maximum_notional_to_equity",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if self.minimum_displacement_body_fraction > 1:
            raise ValueError("minimum_displacement_body_fraction cannot exceed one")
        if self.risk_fraction > 1:
            raise ValueError("risk_fraction cannot exceed one")


@dataclass(frozen=True, slots=True)
class V08IntradayBuildDiagnostics:
    m15_locations: int
    internal_m5_pivot_pairs: int
    internal_sweep_events: int
    context_rejections: int
    episodes_without_prompt_delivery: int
    weak_displacement_rejections: int
    external_liquidity_missing: int
    target_used_before_delivery: int
    target_space_rejections: int
    exposure_rejections: int
    duplicate_scenes_suppressed: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08IntradayBuildResult:
    authorities: tuple[LiquidityDeliveryAuthority, ...]
    diagnostics: V08IntradayBuildDiagnostics


@dataclass(frozen=True, slots=True)
class _LocationSweep:
    location: OrderBlock
    pivot: StrictPivot
    event: LiquidityEvent


def _direction(side: Side) -> float:
    return 1.0 if side is Side.LONG else -1.0


def _ahead(side: Side, price: float, reference: float, tick: float) -> bool:
    return _direction(side) * (price - reference) >= tick - 1e-12


def _detect_internal_m5_sweeps(
    book: FeatureBook,
) -> tuple[tuple[_LocationSweep, ...], int, int]:
    """Find the first reclaim of each confirmed M5 pivot inside an active M15 OB."""

    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    raw: list[_LocationSweep] = []
    pair_count = 0
    context_rejections = 0

    for location in book.order_blocks[Timeframe.M15]:
        pivot_kind = "low" if location.side is Side.LONG else "high"
        for pivot in book.pivots[Timeframe.M5]:
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
            if not _order_block_is_active(book, location, as_of=paired_at):
                continue
            pair_count += 1
            start = max(1, int(frame.index.searchsorted(paired_at, side="left")))
            for index in range(start, len(frame)):
                opened = frame.index[index]
                close_time = closes[index]
                if pivot.known_at > opened or location.known_at > opened:
                    continue
                if not _order_block_is_active(book, location, as_of=close_time):
                    break
                row = frame.iloc[index]
                # The sweep itself must occur at the selected M15 location.
                if not (
                    float(row["low"]) <= location.zone.high + 1e-12
                    and float(row["high"]) >= location.zone.low - 1e-12
                ):
                    continue
                previous_close = float(frame.iloc[index - 1]["close"])
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
                if not _structure_location_side_is_allowed(
                    book,
                    location,
                    as_of=close_time,
                ):
                    context_rejections += 1
                    break
                event = LiquidityEvent(
                    event_id=(
                        f"v08-internal-m5-sweep:{book.symbol}:{location.side.value}:"
                        f"{pivot.pivot_id}:{opened.isoformat()}"
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
                break

    # Nested M15 OBs can describe the same internal sweep.  Preserve one scene,
    # preferring the newest and then narrowest active location.
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
        context_rejections,
    )


def _external_pivot_destination(
    book: FeatureBook,
    event: LiquidityEvent,
    *,
    entry_reference: float,
) -> TargetCandidate | None:
    target_kind = "high" if event.side is Side.LONG else "low"
    candidates: list[StrictPivot] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, event.known_at)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.kind != target_kind
                or pivot.known_at > event.event_time
                or not _ahead(
                    event.side,
                    pivot.price,
                    entry_reference,
                    book.tick_size,
                )
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            candidates.append(pivot)
    if not candidates:
        return None
    pivot = min(
        candidates,
        key=lambda item: (
            abs(item.price - entry_reference),
            0 if item.timeframe is Timeframe.H4 else 1 if item.timeframe is Timeframe.H1 else 2,
            -item.pivot_time.value,
            item.pivot_id,
        ),
    )
    return TargetCandidate(
        candidate_id=f"v08-internal-destination:{pivot.pivot_id}",
        symbol=book.symbol,
        trade_side=event.side,
        kind="pivot",
        zone=PriceZone(pivot.price, pivot.price),
        known_at=pivot.known_at,
        source_id=pivot.pivot_id,
    )


def _displacement_bar(candidate: object):
    order_block = getattr(candidate, "order_block")
    if order_block is not None:
        return order_block.formation_bars[-1]
    gap = getattr(candidate, "fvg")
    assert gap is not None
    return gap.formation_bars[1]


def _displacement_is_material(
    book: FeatureBook,
    candidate: object,
    *,
    policy: V08IntradayPolicy,
) -> bool:
    bar = _displacement_bar(candidate)
    frame = book.frames[Timeframe.M5]
    prior = frame.loc[frame.index < bar.open_time].tail(
        policy.displacement_lookback_bars
    )
    if len(prior) < policy.minimum_displacement_history_bars:
        return False
    ranges = [
        float(high) - float(low)
        for high, low in zip(prior["high"], prior["low"], strict=True)
        if float(high) > float(low)
    ]
    if len(ranges) < policy.minimum_displacement_history_bars:
        return False
    median_range = statistics.median(ranges)
    current_range = bar.high - bar.low
    if median_range <= 0 or current_range <= 0:
        return False
    body_fraction = abs(bar.close - bar.open) / current_range
    directional = bar.bullish if getattr(candidate, "pivot").kind == "high" else bar.bearish
    return (
        directional
        and current_range + 1e-12
        >= median_range * policy.minimum_displacement_range_multiple
        and body_fraction + 1e-12 >= policy.minimum_displacement_body_fraction
    )


def _entry_price(side: Side, zone: PriceZone) -> float:
    return zone.high if side is Side.LONG else zone.low


def _target_r(
    *,
    side: Side,
    entry: float,
    stop: float,
    target: TargetCandidate,
) -> float:
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        return 0.0
    return _direction(side) * (target.order_price - entry) / risk_distance


def _required_notional_to_equity(
    *,
    side: Side,
    entry: float,
    stop: float,
    costs: CostConfig,
    risk_fraction: float,
) -> float:
    fraction = costs.stop_slippage_bps / 10_000
    stop_fill = stop * (1.0 - fraction if side is Side.LONG else 1.0 + fraction)
    unit_risk = (
        abs(entry - stop)
        + abs(stop - stop_fill)
        + entry * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    if unit_risk <= 0:
        return math.inf
    return risk_fraction * entry / unit_risk


def build_v08_intraday_liquidity_delivery_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
) -> V08IntradayBuildResult:
    """Build frequent but causal internal-liquidity delivery scenes."""

    sweeps, pair_count, context_rejections = _detect_internal_m5_sweeps(book)
    m5_blocks = book.order_blocks[Timeframe.M5]
    m5_fvgs = book.fvgs[Timeframe.M5]
    raw: list[LiquidityDeliveryAuthority] = []
    no_prompt_delivery = 0
    weak_displacement = 0
    target_missing = 0
    target_used = 0
    target_space = 0
    exposure_rejections = 0

    for item in sweeps:
        event = item.event
        candidates: list[object] = []
        latest_delivery_time = event.known_at + (
            TIMEFRAME_DELTA[Timeframe.M5] * policy.maximum_delivery_delay_bars
        )
        for block in m5_blocks:
            if block.known_at > latest_delivery_time:
                continue
            candidate = _ob_candidate(
                book,
                event=event,
                location=item.location,
                block=block,
            )
            if candidate is not None:
                candidates.append(candidate)
        for gap in m5_fvgs:
            if gap.known_at > latest_delivery_time:
                continue
            candidate = _fvg_candidate(
                book,
                event=event,
                location=item.location,
                gap=gap,
            )
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            no_prompt_delivery += 1
            continue
        material = [
            candidate
            for candidate in candidates
            if _displacement_is_material(book, candidate, policy=policy)
        ]
        if not material:
            weak_displacement += 1
            continue
        delivery = min(material, key=_candidate_key)
        if not _order_block_is_active(
            book,
            item.location,
            as_of=delivery.known_at,
        ) or not _m5_sweep_episode_is_valid(
            book,
            event,
            until=delivery.known_at,
        ):
            no_prompt_delivery += 1
            continue

        entry = _entry_price(event.side, delivery.zone)
        destination = _external_pivot_destination(
            book,
            event,
            entry_reference=entry,
        )
        if destination is None:
            target_missing += 1
            continue
        if _target_touched(
            book,
            destination,
            after=event.known_at,
            through=delivery.known_at,
        ):
            target_used += 1
            continue
        if _target_r(
            side=event.side,
            entry=entry,
            stop=delivery.initial_stop,
            target=destination,
        ) + 1e-12 < policy.minimum_target_r:
            target_space += 1
            continue
        if _required_notional_to_equity(
            side=event.side,
            entry=entry,
            stop=delivery.initial_stop,
            costs=costs,
            risk_fraction=policy.risk_fraction,
        ) > policy.maximum_notional_to_equity + 1e-12:
            exposure_rejections += 1
            continue

        raw.append(
            LiquidityDeliveryAuthority(
                authority_id=(
                    f"v08-internal-liquidity-delivery:{item.location.ob_id}|"
                    f"{event.event_id}|{delivery.delivery_root_id}|{delivery.kind}"
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

    grouped: dict[tuple[pd.Timestamp, Side, str], list[LiquidityDeliveryAuthority]] = {}
    for authority in raw:
        grouped.setdefault(
            (authority.known_at, authority.side, authority.delivery_root_id),
            [],
        ).append(authority)
    selected = [
        min(
            items,
            key=lambda authority: (
                0
                if authority.entry_zone_source in {
                    "m15_m5_intersection",
                    "ob_fvg_intersection",
                }
                else 1,
                authority.zone.width,
                -authority.liquidity_event.known_at.value,
                -authority.location_ob.known_at.value,
                authority.authority_id,
            ),
        )
        for items in grouped.values()
    ]
    authorities = tuple(
        sorted(selected, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08IntradayBuildResult(
        authorities=authorities,
        diagnostics=V08IntradayBuildDiagnostics(
            m15_locations=len(book.order_blocks[Timeframe.M15]),
            internal_m5_pivot_pairs=pair_count,
            internal_sweep_events=len(sweeps),
            context_rejections=context_rejections,
            episodes_without_prompt_delivery=no_prompt_delivery,
            weak_displacement_rejections=weak_displacement,
            external_liquidity_missing=target_missing,
            target_used_before_delivery=target_used,
            target_space_rejections=target_space,
            exposure_rejections=exposure_rejections,
            duplicate_scenes_suppressed=len(raw) - len(authorities),
            authorities=len(authorities),
        ),
    )


def build_v08_intraday_liquidity_delivery_authorities(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
) -> tuple[LiquidityDeliveryAuthority, ...]:
    return build_v08_intraday_liquidity_delivery_result(
        book,
        costs=costs,
        policy=policy,
    ).authorities


__all__ = [
    "V08IntradayBuildDiagnostics",
    "V08IntradayBuildResult",
    "V08IntradayPolicy",
    "build_v08_intraday_liquidity_delivery_authorities",
    "build_v08_intraday_liquidity_delivery_result",
]
