from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Literal, TypeAlias

import pandas as pd


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"


class ObKind(str, Enum):
    SIMPLE_2C = "simple_2c"
    DOUBLE_3C = "double_3c"


class SceneFamily(str, Enum):
    A1_B1_CONFLUENCE = "a1_b1_confluence"
    PREEXISTING_OB_STRUCTURE_FIRST_RETEST = (
        "preexisting_ob_structure_first_retest"
    )
    M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST = (
        "m15_ob_m5_liquidity_delivery_first_retest"
    )
    OWNED_M15_ANCHOR_OVERLAP_FIRST_RETURN = (
        "owned_m15_anchor_overlap_first_return"
    )
    SR_FLIP_FVG = "sr_flip_fvg"


class EntryMode(str, Enum):
    LIMIT_FIRST_REVISIT = "limit_first_revisit"
    NEXT_BAR_OPEN = "next_bar_open"


class OBCausalState(str, Enum):
    """Whether the execution OB existed before or was born from the event."""

    PREEXISTING = "preexisting"
    EVENT_CREATED = "event_created"


class B1Subtype(str, Enum):
    SWEEP_RECLAIM = "sweep_reclaim"
    BREAK_RETEST = "break_retest"


class ConfirmationModel(str, Enum):
    SAME_TIMEFRAME_EVENT_OB_V0 = "same_timeframe_event_ob.v0"
    M15_LIQUIDITY_M5_MSS_OB_V1 = "m15_liquidity_m5_mss_ob.v1"


PivotKind = Literal["high", "low"]
TargetKind = Literal["impulse", "pivot", "order_block", "fvg"]
DeliveryKind = Literal["ob", "ob_fvg", "fvg"]
DeliveryStopOwner = Literal[
    "m5_ob_formation",
    "m5_fvg_formation",
    "m15_event",
]
OwnedM15StopOwner = Literal[
    "m15_anchor_formation",
    "protected_m15_swing",
]
OwnedM15PairType = Literal["h1_m15", "m15_m5"]
OwnedM15PartnerTiming = Literal["at_anchor_close", "later_fresh"]
EntryZoneSource = Literal[
    "ob_body",
    "fvg_wick_gap",
    "m15_m5_intersection",
    "ob_fvg_intersection",
]


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


@dataclass(frozen=True, slots=True)
class PriceZone:
    low: float
    high: float

    def __post_init__(self) -> None:
        low = _positive(self.low, name="zone low")
        high = _positive(self.high, name="zone high")
        if high < low:
            raise ValueError("zone high cannot be below zone low")
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains(self, price: float) -> bool:
        value = _positive(price, name="price")
        return self.low <= value <= self.high

    def intersects(self, other: PriceZone, *, tolerance: float = 0.0) -> bool:
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("tolerance must be finite and non-negative")
        return max(self.low, other.low) <= min(self.high, other.high) + tolerance


@dataclass(frozen=True, slots=True)
class FormationBar:
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        opened = _utc(self.open_time, name="open_time")
        closed = _utc(self.close_time, name="close_time")
        if closed <= opened:
            raise ValueError("close_time must follow open_time")
        prices = tuple(
            _positive(value, name=name)
            for name, value in (
                ("open", self.open),
                ("high", self.high),
                ("low", self.low),
                ("close", self.close),
            )
        )
        open_price, high, low, close = prices
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise ValueError("invalid OHLC ordering")
        volume = float(self.volume)
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("volume must be finite and non-negative")
        object.__setattr__(self, "open_time", opened)
        object.__setattr__(self, "close_time", closed)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def doji(self) -> bool:
        return self.close == self.open


@dataclass(frozen=True, slots=True)
class OrderBlock:
    ob_id: str
    symbol: str
    timeframe: Timeframe
    kind: ObKind
    side: Side
    formation_bars: tuple[FormationBar, ...]
    zone: PriceZone
    known_at: pd.Timestamp
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float

    def __post_init__(self) -> None:
        if not self.ob_id or not self.symbol:
            raise ValueError("order-block identity fields are required")
        expected_count = 2 if self.kind is ObKind.SIMPLE_2C else 3
        if len(self.formation_bars) != expected_count:
            raise ValueError(f"{self.kind.value} requires {expected_count} formation bars")
        if tuple(sorted(self.formation_bars, key=lambda bar: bar.open_time)) != self.formation_bars:
            raise ValueError("formation bars must be chronological")
        known = _utc(self.known_at, name="known_at")
        if known != self.formation_bars[-1].close_time:
            raise ValueError("order block must be known at its last formation-bar close")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        valid_stop = (
            initial_stop < stop_extreme
            if self.side is Side.LONG
            else initial_stop > stop_extreme
        )
        if not valid_stop:
            raise ValueError("initial stop must be one side beyond the formation extreme")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)


@dataclass(frozen=True, slots=True)
class StrictPivot:
    pivot_id: str
    symbol: str
    timeframe: Timeframe
    kind: PivotKind
    price: float
    pivot_time: pd.Timestamp
    known_at: pd.Timestamp

    def __post_init__(self) -> None:
        if not self.pivot_id or not self.symbol:
            raise ValueError("pivot identity fields are required")
        if self.kind not in {"high", "low"}:
            raise ValueError("pivot kind must be high or low")
        price = _positive(self.price, name="pivot price")
        pivot_time = _utc(self.pivot_time, name="pivot_time")
        known_at = _utc(self.known_at, name="known_at")
        if known_at <= pivot_time:
            raise ValueError("pivot must be known after its pivot bar opens")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "pivot_time", pivot_time)
        object.__setattr__(self, "known_at", known_at)


@dataclass(frozen=True, slots=True)
class FairValueGap:
    fvg_id: str
    symbol: str
    timeframe: Timeframe
    side: Side
    formation_bars: tuple[FormationBar, FormationBar, FormationBar]
    zone: PriceZone
    known_at: pd.Timestamp

    def __post_init__(self) -> None:
        if not self.fvg_id or not self.symbol:
            raise ValueError("FVG identity fields are required")
        if tuple(sorted(self.formation_bars, key=lambda bar: bar.open_time)) != self.formation_bars:
            raise ValueError("FVG formation bars must be chronological")
        known = _utc(self.known_at, name="known_at")
        if known != self.formation_bars[-1].close_time:
            raise ValueError("FVG must be known at its C-bar close")
        object.__setattr__(self, "known_at", known)


@dataclass(frozen=True, slots=True)
class LiquidityEvent:
    event_id: str
    symbol: str
    timeframe: Timeframe
    subtype: B1Subtype
    side: Side
    node_id: str
    node_price: float
    event_time: pd.Timestamp
    known_at: pd.Timestamp
    event_extreme: float | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol or not self.node_id:
            raise ValueError("liquidity-event identity fields are required")
        price = _positive(self.node_price, name="node_price")
        event_time = _utc(self.event_time, name="event_time")
        known_at = _utc(self.known_at, name="known_at")
        if known_at <= event_time:
            raise ValueError("liquidity event must be known after its bar opens")
        event_extreme = (
            price
            if self.event_extreme is None
            else _positive(self.event_extreme, name="event_extreme")
        )
        object.__setattr__(self, "node_price", price)
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "known_at", known_at)
        object.__setattr__(self, "event_extreme", event_extreme)


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    candidate_id: str
    symbol: str
    trade_side: Side
    kind: TargetKind
    zone: PriceZone
    known_at: pd.Timestamp
    source_id: str

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.symbol or not self.source_id:
            raise ValueError("target-candidate identity fields are required")
        if self.kind not in {"impulse", "pivot", "order_block", "fvg"}:
            raise ValueError("unknown target-candidate kind")
        object.__setattr__(self, "known_at", _utc(self.known_at, name="known_at"))

    @property
    def order_price(self) -> float:
        return self.zone.low if self.trade_side is Side.LONG else self.zone.high


@dataclass(frozen=True, slots=True)
class TriggerAuthority:
    authority_id: str
    symbol: str
    subtype: B1Subtype
    side: Side
    timeframes: tuple[Timeframe, ...]
    order_blocks: tuple[OrderBlock, ...]
    zone: PriceZone
    known_at: pd.Timestamp
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    liquidity_event_id: str
    liquidity_node_id: str
    liquidity_node_price: float
    confirmation_model: ConfirmationModel = ConfirmationModel.SAME_TIMEFRAME_EVENT_OB_V0
    liquidity_event_timeframe: Timeframe | None = None
    displacement_pivot_id: str | None = None
    displacement_pivot_price: float | None = None
    liquidity_event_extreme: float | None = None

    def __post_init__(self) -> None:
        if not self.authority_id or not self.symbol:
            raise ValueError("trigger-authority identity fields are required")
        if not self.liquidity_event_id or not self.liquidity_node_id:
            raise ValueError("B1 liquidity-event identity fields are required")
        if len(self.timeframes) != 1 or len(self.order_blocks) != 1:
            raise ValueError("B1 confirmation requires exactly one lower-timeframe order block")
        if self.timeframes[0] not in {Timeframe.M5, Timeframe.M15}:
            raise ValueError("B1 confirmation timeframe must be 5m or 15m")
        if self.order_blocks[0].timeframe is not self.timeframes[0]:
            raise ValueError("B1 confirmation timeframe and order block differ")
        if any(block.symbol != self.symbol for block in self.order_blocks):
            raise ValueError("trigger-authority symbol mismatch")
        if any(block.side is not self.side for block in self.order_blocks):
            raise ValueError("trigger-authority side mismatch")
        event_timeframe = self.liquidity_event_timeframe or self.timeframes[0]
        if self.confirmation_model is ConfirmationModel.M15_LIQUIDITY_M5_MSS_OB_V1:
            if event_timeframe is not Timeframe.M15 or self.timeframes != (Timeframe.M5,):
                raise ValueError("M15 liquidity/M5 delivery confirmation requires a 15m event and 5m OB")
            if not self.displacement_pivot_id or self.displacement_pivot_price is None:
                raise ValueError("M15 liquidity/M5 delivery confirmation requires an owned 5m MSS pivot")
        displacement_price = (
            None
            if self.displacement_pivot_price is None
            else _positive(self.displacement_pivot_price, name="displacement_pivot_price")
        )
        event_extreme = (
            self.liquidity_node_price
            if self.liquidity_event_extreme is None
            else _positive(self.liquidity_event_extreme, name="liquidity_event_extreme")
        )
        object.__setattr__(self, "known_at", _utc(self.known_at, name="known_at"))
        object.__setattr__(self, "stop_extreme", _positive(self.stop_extreme, name="stop_extreme"))
        object.__setattr__(self, "initial_stop", _positive(self.initial_stop, name="initial_stop"))
        object.__setattr__(self, "impulse_extreme", _positive(self.impulse_extreme, name="impulse_extreme"))
        object.__setattr__(
            self,
            "liquidity_node_price",
            _positive(self.liquidity_node_price, name="liquidity_node_price"),
        )
        object.__setattr__(self, "liquidity_event_timeframe", event_timeframe)
        object.__setattr__(self, "displacement_pivot_price", displacement_price)
        object.__setattr__(self, "liquidity_event_extreme", event_extreme)


@dataclass(frozen=True, slots=True)
class ConfluenceAuthority:
    authority_id: str
    symbol: str
    side: Side
    location: OrderBlock | StrictPivot
    confirmation: TriggerAuthority
    zone: PriceZone
    known_at: pd.Timestamp
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    destination: TargetCandidate | None = None

    def __post_init__(self) -> None:
        if not self.authority_id or not self.symbol:
            raise ValueError("confluence-authority identity fields are required")
        if self.location.symbol != self.symbol or self.confirmation.symbol != self.symbol:
            raise ValueError("confluence-authority symbol mismatch")
        if self.confirmation.side is not self.side:
            raise ValueError("confluence-authority side mismatch")
        pair = (self.location.timeframe, self.confirmation.timeframes[0])
        if isinstance(self.location, OrderBlock):
            if self.location.side is not self.side:
                raise ValueError("confluence-authority side mismatch")
            if pair not in {
                (Timeframe.H1, Timeframe.M15),
                (Timeframe.H1, Timeframe.M5),
                (Timeframe.M15, Timeframe.M5),
            }:
                raise ValueError("unsupported OB-location/B1 timeframe pair")
        else:
            if self.location.timeframe is not Timeframe.H1 or pair not in {
                (Timeframe.H1, Timeframe.M15),
                (Timeframe.H1, Timeframe.M5),
            }:
                raise ValueError("pivot A1 must be a strict 1H liquidity location")
            if self.location.pivot_id != self.confirmation.liquidity_node_id:
                raise ValueError("pivot A1 must own the B1 liquidity event")
        if self.confirmation.known_at <= self.location.known_at:
            raise ValueError("B1 confirmation must complete after the A1 location exists")
        if not self.zone.intersects(self.confirmation.zone):
            raise ValueError("entry zone must be owned by the B1 confirmation")
        known_at = _utc(self.known_at, name="known_at")
        if known_at != self.confirmation.known_at:
            raise ValueError("confluence becomes known at the B1 confirmation close")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        valid_stop = (
            initial_stop < stop_extreme
            if self.side is Side.LONG
            else initial_stop > stop_extreme
        )
        if not valid_stop:
            raise ValueError("confluence initial stop is on the wrong side")
        object.__setattr__(self, "known_at", known_at)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)

    @property
    def scene_family(self) -> SceneFamily:
        return SceneFamily.A1_B1_CONFLUENCE

    @property
    def ob_causal_state(self) -> OBCausalState:
        if self.confirmation.confirmation_model is ConfirmationModel.M15_LIQUIDITY_M5_MSS_OB_V1:
            return OBCausalState.EVENT_CREATED
        return OBCausalState.PREEXISTING

    @property
    def entry_mode(self) -> EntryMode:
        if self.ob_causal_state is OBCausalState.EVENT_CREATED:
            return EntryMode.NEXT_BAR_OPEN
        return EntryMode.LIMIT_FIRST_REVISIT

    @property
    def location_id(self) -> str:
        return (
            self.location.ob_id
            if isinstance(self.location, OrderBlock)
            else self.location.pivot_id
        )

    @property
    def has_literal_body_overlap(self) -> bool:
        return isinstance(self.location, OrderBlock) and self.location.zone.intersects(
            self.confirmation.zone
        )

@dataclass(frozen=True, slots=True)
class StructureFlipAuthority:
    """Preexisting M15 OB plus a later, separately completed M5 structure break."""

    authority_id: str
    symbol: str
    side: Side
    location_ob: OrderBlock
    refinement_ob: OrderBlock | None
    break_pivot: StrictPivot
    break_bar: FormationBar
    zone: PriceZone
    known_at: pd.Timestamp
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    destination: TargetCandidate
    liquidity_event_id: str | None = None
    liquidity_node_id: str | None = None

    def __post_init__(self) -> None:
        if not self.authority_id or not self.symbol:
            raise ValueError("structure-flip authority identity fields are required")
        if self.location_ob.symbol != self.symbol or self.break_pivot.symbol != self.symbol:
            raise ValueError("structure-flip authority symbol mismatch")
        if self.location_ob.timeframe is not Timeframe.M15:
            raise ValueError("structure-flip location must be a 15m OB")
        if self.location_ob.side is not self.side:
            raise ValueError("structure-flip side mismatch")
        if self.refinement_ob is not None:
            if self.refinement_ob.timeframe is not Timeframe.M5:
                raise ValueError("structure-flip refinement must be a 5m OB")
            if self.refinement_ob.side is not self.side:
                raise ValueError("structure-flip refinement side mismatch")
            if not self.location_ob.zone.intersects(self.refinement_ob.zone):
                raise ValueError("structure-flip refinement must overlap the 15m OB")
        known = _utc(self.known_at, name="known_at")
        if known != self.break_bar.close_time:
            raise ValueError("structure flip becomes known at the break-bar close")
        if self.break_pivot.known_at > self.break_bar.open_time:
            raise ValueError("structure break cannot use a pivot unknown at bar open")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        if (self.side is Side.LONG and initial_stop >= stop_extreme) or (
            self.side is Side.SHORT and initial_stop <= stop_extreme
        ):
            raise ValueError("structure-flip initial stop is on the wrong side")
        if self.destination.trade_side is not self.side:
            raise ValueError("structure-flip destination side mismatch")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)

    @property
    def scene_family(self) -> SceneFamily:
        return SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST

    @property
    def ob_causal_state(self) -> OBCausalState:
        return OBCausalState.PREEXISTING

    @property
    def entry_mode(self) -> EntryMode:
        return EntryMode.LIMIT_FIRST_REVISIT

    @property
    def location_id(self) -> str:
        return self.location_ob.ob_id

    @property
    def execution_ob(self) -> OrderBlock:
        return self.refinement_ob or self.location_ob

    @property
    def has_literal_body_overlap(self) -> bool:
        return self.refinement_ob is not None


@dataclass(frozen=True, slots=True)
class LiquidityDeliveryAuthority:
    """M15 OB location, M5 liquidity sweep, and owned M5 displacement zone."""

    authority_id: str
    symbol: str
    side: Side
    location_ob: OrderBlock
    liquidity_event: LiquidityEvent
    delivery_kind: DeliveryKind
    delivery_root_id: str
    displacement_pivot: StrictPivot
    delivery_ob: OrderBlock | None
    delivery_fvg: FairValueGap | None
    zone: PriceZone
    entry_zone_source: EntryZoneSource
    known_at: pd.Timestamp
    stop_owner: DeliveryStopOwner
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    destination: TargetCandidate

    def __post_init__(self) -> None:
        if not self.authority_id or not self.symbol or not self.delivery_root_id:
            raise ValueError("liquidity-delivery authority identity fields are required")
        if self.location_ob.symbol != self.symbol:
            raise ValueError("liquidity-delivery location symbol mismatch")
        if self.location_ob.timeframe is not Timeframe.M15:
            raise ValueError("liquidity-delivery location must be a 15m OB")
        if self.location_ob.side is not self.side:
            raise ValueError("liquidity-delivery location side mismatch")
        if (
            self.liquidity_event.symbol != self.symbol
            or self.liquidity_event.timeframe is not Timeframe.M5
            or self.liquidity_event.side is not self.side
        ):
            raise ValueError("liquidity-delivery event mismatch")
        if self.liquidity_event.known_at <= self.location_ob.known_at:
            raise ValueError("M5 liquidity event must follow the M15 location")
        if (
            self.displacement_pivot.symbol != self.symbol
            or self.displacement_pivot.timeframe is not Timeframe.M5
        ):
            raise ValueError("liquidity-delivery displacement pivot mismatch")
        expected_pivot = "high" if self.side is Side.LONG else "low"
        if self.displacement_pivot.kind != expected_pivot:
            raise ValueError("liquidity-delivery displacement pivot is on the wrong side")
        if self.delivery_kind == "ob":
            if self.delivery_ob is None or self.delivery_fvg is not None:
                raise ValueError("OB delivery requires only an order block")
        elif self.delivery_kind == "ob_fvg":
            if self.delivery_ob is None or self.delivery_fvg is None:
                raise ValueError("OB/FVG delivery requires both objects")
        elif self.delivery_kind == "fvg":
            if self.delivery_fvg is None or self.delivery_ob is not None:
                raise ValueError("FVG delivery requires only an FVG")
        else:
            raise ValueError("unknown liquidity-delivery kind")
        owners = tuple(
            item for item in (self.delivery_ob, self.delivery_fvg) if item is not None
        )
        if any(item.symbol != self.symbol or item.timeframe is not Timeframe.M5 for item in owners):
            raise ValueError("liquidity-delivery owner mismatch")
        if any(item.side is not self.side for item in owners):
            raise ValueError("liquidity-delivery owner side mismatch")
        if any(item.known_at <= self.liquidity_event.known_at for item in owners):
            raise ValueError("liquidity-delivery owner must follow its event")
        known = _utc(self.known_at, name="known_at")
        owner_known = max(item.known_at for item in owners)
        if known != owner_known:
            raise ValueError("liquidity delivery becomes known with its final owner")
        if not all(
            self.zone.low >= item.zone.low - 1e-12
            and self.zone.high <= item.zone.high + 1e-12
            for item in owners
        ):
            raise ValueError("entry zone must be owned by the delivery object")
        if self.entry_zone_source not in {
            "ob_body",
            "fvg_wick_gap",
            "m15_m5_intersection",
            "ob_fvg_intersection",
        }:
            raise ValueError("unknown liquidity-delivery entry zone source")
        if self.stop_owner not in {
            "m5_ob_formation",
            "m5_fvg_formation",
            "m15_event",
        }:
            raise ValueError("unknown liquidity-delivery stop owner")
        if self.stop_owner == "m5_ob_formation" and self.delivery_ob is None:
            raise ValueError("M5 OB stop owner requires an OB delivery")
        if self.stop_owner == "m5_fvg_formation" and self.delivery_fvg is None:
            raise ValueError("M5 FVG stop owner requires an FVG delivery")
        stop_bars = (
            self.delivery_ob.formation_bars
            if self.stop_owner == "m5_ob_formation"
            else self.delivery_fvg.formation_bars
            if self.stop_owner == "m5_fvg_formation"
            else ()
        )
        if stop_bars and not (
            min(item.low for item in stop_bars) - 1e-12
            <= self.liquidity_event.event_extreme
            <= max(item.high for item in stop_bars) + 1e-12
        ):
            raise ValueError("delivery formation cannot own an uncovered event extreme")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        if (self.side is Side.LONG and initial_stop >= stop_extreme) or (
            self.side is Side.SHORT and initial_stop <= stop_extreme
        ):
            raise ValueError("liquidity-delivery initial stop is on the wrong side")
        expected_stop_extreme = (
            self.liquidity_event.event_extreme
            if self.stop_owner == "m15_event"
            else min(item.low for item in stop_bars)
            if self.side is Side.LONG
            else max(item.high for item in stop_bars)
        )
        if not math.isclose(stop_extreme, expected_stop_extreme):
            raise ValueError("liquidity-delivery stop extreme disagrees with its owner")
        if self.destination.trade_side is not self.side:
            raise ValueError("liquidity-delivery destination side mismatch")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)

    @property
    def scene_family(self) -> SceneFamily:
        return SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST

    @property
    def ob_causal_state(self) -> OBCausalState:
        return OBCausalState.EVENT_CREATED

    @property
    def entry_mode(self) -> EntryMode:
        return EntryMode.LIMIT_FIRST_REVISIT

    @property
    def location_id(self) -> str:
        return self.location_ob.ob_id

    @property
    def execution_id(self) -> str:
        if self.delivery_ob is not None:
            return self.delivery_ob.ob_id
        assert self.delivery_fvg is not None
        return self.delivery_fvg.fvg_id

    @property
    def has_literal_body_overlap(self) -> bool:
        return self.delivery_kind == "ob_fvg"


@dataclass(frozen=True, slots=True)
class OwnedM15OverlapAuthority:
    """M15 OB whose final bar owns a break, paired with an H1/M5 OB."""

    authority_id: str
    scene_root_id: str
    symbol: str
    side: Side
    anchor_ob: OrderBlock
    partner_ob: OrderBlock
    pair_type: OwnedM15PairType
    partner_timing: OwnedM15PartnerTiming
    break_pivot: StrictPivot
    protected_pivot: StrictPivot
    break_bar: FormationBar
    zone: PriceZone
    pair_known_at: pd.Timestamp
    departure_bar: FormationBar
    known_at: pd.Timestamp
    stop_owner: OwnedM15StopOwner
    stop_extreme: float
    initial_stop: float
    impulse_extreme: float
    destination: TargetCandidate

    def __post_init__(self) -> None:
        if not self.authority_id or not self.scene_root_id or not self.symbol:
            raise ValueError("owned-overlap identity fields are required")
        if (
            self.anchor_ob.symbol != self.symbol
            or self.partner_ob.symbol != self.symbol
            or self.break_pivot.symbol != self.symbol
            or self.protected_pivot.symbol != self.symbol
        ):
            raise ValueError("owned-overlap symbol mismatch")
        if self.anchor_ob.timeframe is not Timeframe.M15:
            raise ValueError("owned-overlap anchor must be a 15m OB")
        if self.pair_type not in {"h1_m15", "m15_m5"}:
            raise ValueError("unknown owned-overlap pair type")
        expected_partner = (
            Timeframe.H1 if self.pair_type == "h1_m15" else Timeframe.M5
        )
        if self.partner_ob.timeframe is not expected_partner:
            raise ValueError("owned-overlap partner disagrees with pair type")
        if self.anchor_ob.side is not self.side or self.partner_ob.side is not self.side:
            raise ValueError("owned-overlap side mismatch")
        if self.break_bar != self.anchor_ob.formation_bars[-1]:
            raise ValueError("owned break bar must be the anchor's final formation bar")
        expected_break_kind = "high" if self.side is Side.LONG else "low"
        expected_protected_kind = "low" if self.side is Side.LONG else "high"
        if (
            self.break_pivot.timeframe is not Timeframe.M15
            or self.break_pivot.kind != expected_break_kind
            or self.break_pivot.known_at > self.break_bar.open_time
        ):
            raise ValueError("owned break pivot is invalid")
        if (
            self.protected_pivot.timeframe is not Timeframe.M15
            or self.protected_pivot.kind != expected_protected_kind
            or self.protected_pivot.known_at > self.break_bar.open_time
        ):
            raise ValueError("protected M15 pivot is invalid")
        if not (
            self.zone.low >= self.anchor_ob.zone.low - 1e-12
            and self.zone.high <= self.anchor_ob.zone.high + 1e-12
            and self.zone.low >= self.partner_ob.zone.low - 1e-12
            and self.zone.high <= self.partner_ob.zone.high + 1e-12
        ):
            raise ValueError("entry zone must be the anchor/partner intersection")
        pair_known = _utc(self.pair_known_at, name="pair_known_at")
        expected_pair_known = max(self.anchor_ob.known_at, self.partner_ob.known_at)
        if pair_known != expected_pair_known:
            raise ValueError("pair_known_at must equal the final OB confirmation time")
        known = _utc(self.known_at, name="known_at")
        if known != self.departure_bar.close_time or known < pair_known:
            raise ValueError("owned-overlap becomes known at its departure close")
        if self.partner_timing == "at_anchor_close":
            if self.partner_ob.known_at > self.anchor_ob.known_at:
                raise ValueError("at-anchor partner cannot be confirmed later")
        elif self.partner_timing == "later_fresh":
            if self.partner_ob.known_at <= self.anchor_ob.known_at:
                raise ValueError("later partner must be confirmed after the anchor")
        else:
            raise ValueError("unknown owned-overlap partner timing")
        stop_extreme = _positive(self.stop_extreme, name="stop_extreme")
        initial_stop = _positive(self.initial_stop, name="initial_stop")
        impulse_extreme = _positive(self.impulse_extreme, name="impulse_extreme")
        expected_stop_extreme = (
            min(bar.low for bar in self.anchor_ob.formation_bars)
            if self.stop_owner == "m15_anchor_formation" and self.side is Side.LONG
            else max(bar.high for bar in self.anchor_ob.formation_bars)
            if self.stop_owner == "m15_anchor_formation"
            else self.protected_pivot.price
            if self.stop_owner == "protected_m15_swing"
            else None
        )
        if expected_stop_extreme is None:
            raise ValueError("unknown owned-overlap stop owner")
        if not math.isclose(stop_extreme, expected_stop_extreme):
            raise ValueError("owned-overlap stop extreme disagrees with its owner")
        entry = self.zone.high if self.side is Side.LONG else self.zone.low
        if (self.side is Side.LONG and not initial_stop < entry) or (
            self.side is Side.SHORT and not initial_stop > entry
        ):
            raise ValueError("owned-overlap stop is on the wrong side of entry")
        if self.destination.trade_side is not self.side:
            raise ValueError("owned-overlap destination side mismatch")
        if self.destination.known_at > self.anchor_ob.known_at:
            raise ValueError("owned-overlap target must exist when the anchor is born")
        object.__setattr__(self, "pair_known_at", pair_known)
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "stop_extreme", stop_extreme)
        object.__setattr__(self, "initial_stop", initial_stop)
        object.__setattr__(self, "impulse_extreme", impulse_extreme)

    @property
    def scene_family(self) -> SceneFamily:
        return SceneFamily.OWNED_M15_ANCHOR_OVERLAP_FIRST_RETURN

    @property
    def ob_causal_state(self) -> OBCausalState:
        return OBCausalState.EVENT_CREATED

    @property
    def entry_mode(self) -> EntryMode:
        return EntryMode.LIMIT_FIRST_REVISIT

    @property
    def location_id(self) -> str:
        return self.anchor_ob.ob_id

    @property
    def has_literal_body_overlap(self) -> bool:
        return True


SceneAuthority: TypeAlias = (
    ConfluenceAuthority
    | StructureFlipAuthority
    | LiquidityDeliveryAuthority
    | OwnedM15OverlapAuthority
)



