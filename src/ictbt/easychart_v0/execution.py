from __future__ import annotations

from dataclasses import dataclass, replace
import math
from statistics import median
from typing import Literal, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from .domain import (
    EntryMode,
    FormationBar,
    OBCausalState,
    SceneFamily,
    Side,
    Timeframe,
)


ExitReason = Literal["partial_1r", "initial_target", "initial_stop", "volume_spike"]


DEFAULT_RISK_FRACTION = 0.03
DEFAULT_DAILY_LOSS_LIMIT_ENABLED = False
DEFAULT_DAILY_LOSS_LIMIT_FRACTION = 0.01
DEFAULT_DAILY_RESET_TIMEZONE = "Asia/Seoul"


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


def _non_negative(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _floor_step(value: float, step: float) -> float:
    return math.floor((value + 1e-12) / step) * step


def _direction(side: Side) -> float:
    return 1.0 if side is Side.LONG else -1.0


def _directional_distance(side: Side, start: float, end: float) -> float:
    return _direction(side) * (end - start)


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Simple linear-contract costs expressed as decimal rates and bps."""

    entry_fee_rate: float
    stop_fee_rate: float
    target_fee_rate: float
    volume_exit_fee_rate: float
    stop_slippage_bps: float = 0.0
    volume_exit_slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "entry_fee_rate",
            "stop_fee_rate",
            "target_fee_rate",
            "volume_exit_fee_rate",
        ):
            value = _non_negative(getattr(self, name), name=name)
            if value >= 1:
                raise ValueError(f"{name} must be below one")
            object.__setattr__(self, name, value)
        for name in ("stop_slippage_bps", "volume_exit_slippage_bps"):
            value = _non_negative(getattr(self, name), name=name)
            if value >= 10_000:
                raise ValueError(f"{name} must be below 10000")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    risk_fraction: float = DEFAULT_RISK_FRACTION
    quantity_step: float = 0.001
    minimum_quantity: float = 0.0
    minimum_notional: float = 0.0
    daily_loss_limit_enabled: bool = DEFAULT_DAILY_LOSS_LIMIT_ENABLED
    daily_loss_limit_fraction: float = DEFAULT_DAILY_LOSS_LIMIT_FRACTION
    daily_reset_timezone: str = DEFAULT_DAILY_RESET_TIMEZONE

    def __post_init__(self) -> None:
        fraction = _positive(self.risk_fraction, name="risk_fraction")
        if fraction > 1:
            raise ValueError("risk_fraction must be at most one")
        object.__setattr__(self, "risk_fraction", fraction)
        object.__setattr__(
            self, "quantity_step", _positive(self.quantity_step, name="quantity_step")
        )
        object.__setattr__(
            self,
            "minimum_quantity",
            _non_negative(self.minimum_quantity, name="minimum_quantity"),
        )
        object.__setattr__(
            self,
            "minimum_notional",
            _non_negative(self.minimum_notional, name="minimum_notional"),
        )
        object.__setattr__(
            self,
            "daily_loss_limit_enabled",
            bool(self.daily_loss_limit_enabled),
        )
        daily_fraction = _positive(
            self.daily_loss_limit_fraction,
            name="daily_loss_limit_fraction",
        )
        if daily_fraction > 1:
            raise ValueError("daily_loss_limit_fraction must be at most one")
        object.__setattr__(self, "daily_loss_limit_fraction", daily_fraction)
        timezone = str(self.daily_reset_timezone).strip()
        if not timezone:
            raise ValueError("daily_reset_timezone is required")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("daily_reset_timezone must be a valid IANA timezone") from exc
        object.__setattr__(self, "daily_reset_timezone", timezone)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: str
    source_id: str
    symbol: str
    scene_family: SceneFamily
    side: Side
    entry_mode: EntryMode
    created_at: pd.Timestamp
    entry_reference: float
    initial_stop: float
    initial_target: float
    risk_budget: float
    unit_stop_risk: float
    quantity: float
    ob_causal_state: OBCausalState = OBCausalState.PREEXISTING
    quantity_step: float = 0.001
    minimum_quantity: float = 0.0
    minimum_notional: float = 0.0

    def __post_init__(self) -> None:
        if not self.order_id or not self.source_id or not self.symbol:
            raise ValueError("order identity fields are required")
        created = _utc(self.created_at, name="created_at")
        entry = _positive(self.entry_reference, name="entry_reference")
        stop = _positive(self.initial_stop, name="initial_stop")
        target = _positive(self.initial_target, name="initial_target")
        valid_geometry = (
            stop < entry < target
            if self.side is Side.LONG
            else target < entry < stop
        )
        if not valid_geometry:
            raise ValueError("stop, entry, and target geometry is invalid")
        if self.scene_family not in {
            SceneFamily.A1_B1_CONFLUENCE,
            SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST,
            SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
            SceneFamily.OWNED_M15_ANCHOR_OVERLAP_FIRST_RETURN,
            SceneFamily.SR_FLIP_FVG,
        }:
            raise ValueError("unsupported scene family")
        if (
            self.entry_mode is EntryMode.NEXT_BAR_OPEN
            and self.ob_causal_state is not OBCausalState.EVENT_CREATED
        ):
            raise ValueError("next-bar-open entry requires an event-created OB")
        if (
            self.ob_causal_state is OBCausalState.PREEXISTING
            and self.entry_mode is not EntryMode.LIMIT_FIRST_REVISIT
        ):
            raise ValueError("a preexisting OB requires a first-revisit limit entry")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "entry_reference", entry)
        object.__setattr__(self, "initial_stop", stop)
        object.__setattr__(self, "initial_target", target)
        object.__setattr__(self, "risk_budget", _positive(self.risk_budget, name="risk_budget"))
        object.__setattr__(
            self, "unit_stop_risk", _positive(self.unit_stop_risk, name="unit_stop_risk")
        )
        object.__setattr__(self, "quantity", _positive(self.quantity, name="quantity"))
        object.__setattr__(
            self, "quantity_step", _positive(self.quantity_step, name="quantity_step")
        )
        object.__setattr__(
            self,
            "minimum_quantity",
            _non_negative(self.minimum_quantity, name="minimum_quantity"),
        )
        object.__setattr__(
            self,
            "minimum_notional",
            _non_negative(self.minimum_notional, name="minimum_notional"),
        )


@dataclass(frozen=True, slots=True)
class ExitLeg:
    reason: ExitReason
    filled_at: pd.Timestamp
    fill_price: float
    quantity: float
    gross_pnl: float
    fee_paid: float

    def __post_init__(self) -> None:
        if self.reason not in {
            "partial_1r",
            "initial_target",
            "initial_stop",
            "volume_spike",
        }:
            raise ValueError("unknown exit reason")
        object.__setattr__(self, "filled_at", _utc(self.filled_at, name="filled_at"))
        object.__setattr__(self, "fill_price", _positive(self.fill_price, name="fill_price"))
        object.__setattr__(self, "quantity", _positive(self.quantity, name="quantity"))
        if not math.isfinite(self.gross_pnl):
            raise ValueError("gross_pnl must be finite")
        object.__setattr__(self, "fee_paid", _non_negative(self.fee_paid, name="fee_paid"))

    @property
    def net_pnl_before_entry_fee(self) -> float:
        return self.gross_pnl - self.fee_paid


@dataclass(frozen=True, slots=True)
class VolumeExitReservation:
    timeframe: Timeframe
    signal_bar_close: pd.Timestamp
    signal_close: float
    relative_volume: float

    def __post_init__(self) -> None:
        if self.timeframe not in {Timeframe.M5, Timeframe.M15}:
            raise ValueError("volume exit only uses completed 5m or 15m bars")
        object.__setattr__(
            self,
            "signal_bar_close",
            _utc(self.signal_bar_close, name="signal_bar_close"),
        )
        object.__setattr__(self, "signal_close", _positive(self.signal_close, name="signal_close"))
        object.__setattr__(
            self, "relative_volume", _positive(self.relative_volume, name="relative_volume")
        )


@dataclass(frozen=True, slots=True)
class OpenPosition:
    intent: OrderIntent
    filled_at: pd.Timestamp
    entry_price: float
    original_quantity: float
    remaining_quantity: float
    initial_stop: float
    stop_price: float
    initial_target: float
    r_price: float
    target_r: float
    partial_price: float | None
    partial_filled: bool
    entry_fee_paid: float
    exit_legs: tuple[ExitLeg, ...] = ()
    volume_exit: VolumeExitReservation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filled_at", _utc(self.filled_at, name="filled_at"))
        for name in (
            "entry_price",
            "original_quantity",
            "remaining_quantity",
            "initial_stop",
            "stop_price",
            "initial_target",
            "r_price",
            "target_r",
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), name=name))
        if self.remaining_quantity > self.original_quantity + 1e-12:
            raise ValueError("remaining quantity cannot exceed original quantity")
        if self.partial_price is not None:
            object.__setattr__(
                self, "partial_price", _positive(self.partial_price, name="partial_price")
            )
        if self.partial_filled:
            if self.partial_price is None:
                raise ValueError("a partial fill requires a partial price")
            if not math.isclose(self.stop_price, self.entry_price, abs_tol=1e-12):
                raise ValueError("the remaining stop must equal actual entry after partial fill")
        elif not math.isclose(self.stop_price, self.initial_stop, abs_tol=1e-12):
            raise ValueError("the stop cannot change before a partial fill")
        object.__setattr__(
            self, "entry_fee_paid", _non_negative(self.entry_fee_paid, name="entry_fee_paid")
        )

    @property
    def uses_partial_path(self) -> bool:
        return self.partial_price is not None


@dataclass(frozen=True, slots=True)
class TradeRecord:
    order_id: str
    symbol: str
    side: Side
    scene_family: SceneFamily
    entry_time: pd.Timestamp
    entry_price: float
    initial_stop: float
    initial_target: float
    original_quantity: float
    target_r: float
    exit_legs: tuple[ExitLeg, ...]
    entry_fee_paid: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    closed_at: pd.Timestamp
    final_reason: ExitReason


def _adverse_stop_fill(side: Side, stop_price: float, costs: CostConfig) -> float:
    fraction = costs.stop_slippage_bps / 10_000
    return stop_price * (1 - fraction if side is Side.LONG else 1 + fraction)


def _adverse_volume_fill(side: Side, reference: float, costs: CostConfig) -> float:
    fraction = costs.volume_exit_slippage_bps / 10_000
    return reference * (1 - fraction if side is Side.LONG else 1 + fraction)


def calculate_order_quantity(
    *,
    equity: float,
    side: Side,
    entry_reference: float,
    initial_stop: float,
    costs: CostConfig,
    risk: RiskConfig,
) -> tuple[float, float, float]:
    """Return quantity, cash risk budget, and all-in stop risk per unit."""

    account_equity = _positive(equity, name="equity")
    entry = _positive(entry_reference, name="entry_reference")
    stop = _positive(initial_stop, name="initial_stop")
    if (side is Side.LONG and stop >= entry) or (side is Side.SHORT and stop <= entry):
        raise ValueError("initial stop is on the wrong side of entry")
    stop_fill = _adverse_stop_fill(side, stop, costs)
    raw_distance = abs(entry - stop)
    stop_slippage = abs(stop - stop_fill)
    unit_risk = (
        raw_distance
        + stop_slippage
        + entry * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    budget = account_equity * risk.risk_fraction
    quantity = _floor_step(budget / unit_risk, risk.quantity_step)
    if quantity <= 0 or quantity < risk.minimum_quantity - 1e-12:
        raise ValueError("sized quantity is below the minimum quantity")
    if quantity * entry < risk.minimum_notional - 1e-12:
        raise ValueError("sized quantity is below the minimum notional")
    return quantity, budget, unit_risk


def _build_intent(
    *,
    order_id: str,
    source_id: str,
    symbol: str,
    scene_family: SceneFamily,
    side: Side,
    entry_mode: EntryMode,
    created_at: object,
    entry_reference: float,
    initial_stop: float,
    initial_target: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    ob_causal_state: OBCausalState = OBCausalState.PREEXISTING,
) -> OrderIntent:
    target = _positive(initial_target, name="initial_target")
    entry = _positive(entry_reference, name="entry_reference")
    if _directional_distance(side, entry, target) <= 0:
        raise ValueError("initial target is on the wrong side of entry")
    quantity, budget, unit_risk = calculate_order_quantity(
        equity=equity,
        side=side,
        entry_reference=entry,
        initial_stop=initial_stop,
        costs=costs,
        risk=risk,
    )
    return OrderIntent(
        order_id=order_id,
        source_id=source_id,
        symbol=symbol,
        scene_family=scene_family,
        side=side,
        entry_mode=entry_mode,
        created_at=_utc(created_at, name="created_at"),
        entry_reference=entry,
        initial_stop=initial_stop,
        initial_target=target,
        risk_budget=budget,
        unit_stop_risk=unit_risk,
        quantity=quantity,
        ob_causal_state=ob_causal_state,
        quantity_step=risk.quantity_step,
        minimum_quantity=risk.minimum_quantity,
        minimum_notional=risk.minimum_notional,
    )


def build_confluence_intent(
    *,
    order_id: str,
    source_id: str,
    symbol: str,
    side: Side,
    created_at: object,
    entry_reference: float,
    initial_stop: float,
    initial_target: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    entry_mode: EntryMode = EntryMode.LIMIT_FIRST_REVISIT,
    ob_causal_state: OBCausalState = OBCausalState.PREEXISTING,
    scene_family: SceneFamily = SceneFamily.A1_B1_CONFLUENCE,
) -> OrderIntent:
    """Build a confluence intent with an explicit OB clock and entry mode.

    A preexisting OB always keeps the resting first-revisit limit semantics.
    ``NEXT_BAR_OPEN`` is available only for an OB created by the causal event,
    which prevents it from becoming a blanket confirmation-open policy.
    """

    return _build_intent(
        order_id=order_id,
        source_id=source_id,
        symbol=symbol,
        scene_family=scene_family,
        side=side,
        entry_mode=entry_mode,
        created_at=created_at,
        entry_reference=entry_reference,
        initial_stop=initial_stop,
        initial_target=initial_target,
        equity=equity,
        costs=costs,
        risk=risk,
        ob_causal_state=ob_causal_state,
    )


def build_confluence_first_revisit_intent(
    *,
    order_id: str,
    source_id: str,
    symbol: str,
    side: Side,
    created_at: object,
    limit_price: float,
    initial_stop: float,
    initial_target: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
) -> OrderIntent:
    return build_confluence_intent(
        order_id=order_id,
        source_id=source_id,
        symbol=symbol,
        side=side,
        created_at=created_at,
        entry_reference=limit_price,
        initial_stop=initial_stop,
        initial_target=initial_target,
        equity=equity,
        costs=costs,
        risk=risk,
        entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
        ob_causal_state=OBCausalState.PREEXISTING,
    )


def _reprice_next_bar_open_intent(
    intent: OrderIntent,
    *,
    actual_fill_price: float,
    costs: CostConfig,
) -> OrderIntent:
    """Freeze actual next-open quantity without exceeding planned cash risk."""

    if intent.entry_mode is not EntryMode.NEXT_BAR_OPEN:
        return intent
    entry = _positive(actual_fill_price, name="actual_fill_price")
    stop_fill = _adverse_stop_fill(intent.side, intent.initial_stop, costs)
    unit_risk = (
        abs(entry - intent.initial_stop)
        + abs(intent.initial_stop - stop_fill)
        + entry * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    quantity = _floor_step(intent.risk_budget / unit_risk, intent.quantity_step)
    if quantity <= 0 or quantity < intent.minimum_quantity - 1e-12:
        raise ValueError("actual next-open quantity is below the minimum quantity")
    if quantity * entry < intent.minimum_notional - 1e-12:
        raise ValueError("actual next-open quantity is below the minimum notional")
    return replace(
        intent,
        entry_reference=entry,
        unit_stop_risk=unit_risk,
        quantity=quantity,
    )


def open_position_from_fill(
    intent: OrderIntent,
    *,
    actual_fill_price: float,
    filled_at: object,
    costs: CostConfig,
) -> OpenPosition:
    entry = _positive(actual_fill_price, name="actual_fill_price")
    valid_geometry = (
        intent.initial_stop < entry < intent.initial_target
        if intent.side is Side.LONG
        else intent.initial_target < entry < intent.initial_stop
    )
    if not valid_geometry:
        raise ValueError("actual fill destroys stop/target geometry")
    r_price = abs(entry - intent.initial_stop)
    target_r = _directional_distance(intent.side, entry, intent.initial_target) / r_price
    partial_price = None
    if target_r + 1e-12 >= 1.4:
        partial_price = entry + _direction(intent.side) * r_price
    return OpenPosition(
        intent=intent,
        filled_at=_utc(filled_at, name="filled_at"),
        entry_price=entry,
        original_quantity=intent.quantity,
        remaining_quantity=intent.quantity,
        initial_stop=intent.initial_stop,
        stop_price=intent.initial_stop,
        initial_target=intent.initial_target,
        r_price=r_price,
        target_r=target_r,
        partial_price=partial_price,
        partial_filled=False,
        entry_fee_paid=entry * intent.quantity * costs.entry_fee_rate,
    )


def _exit_leg(
    position: OpenPosition,
    *,
    reason: ExitReason,
    filled_at: object,
    fill_price: float,
    quantity: float,
    fee_rate: float,
) -> ExitLeg:
    price = _positive(fill_price, name="fill_price")
    size = _positive(quantity, name="quantity")
    gross = _directional_distance(position.intent.side, position.entry_price, price) * size
    return ExitLeg(
        reason=reason,
        filled_at=_utc(filled_at, name="filled_at"),
        fill_price=price,
        quantity=size,
        gross_pnl=gross,
        fee_paid=price * size * fee_rate,
    )


def fill_partial_1r(
    position: OpenPosition, *, filled_at: object, costs: CostConfig
) -> OpenPosition:
    if position.partial_price is None:
        raise ValueError("this position does not use the partial path")
    if position.partial_filled:
        raise ValueError("the 1R partial was already filled")
    quantity = position.original_quantity * 0.5
    leg = _exit_leg(
        position,
        reason="partial_1r",
        filled_at=filled_at,
        fill_price=position.partial_price,
        quantity=quantity,
        fee_rate=costs.target_fee_rate,
    )
    return replace(
        position,
        remaining_quantity=position.original_quantity - quantity,
        stop_price=position.entry_price,
        partial_filled=True,
        exit_legs=position.exit_legs + (leg,),
    )


def _close_position(position: OpenPosition, leg: ExitLeg) -> TradeRecord:
    legs = position.exit_legs + (leg,)
    gross = sum(item.gross_pnl for item in legs)
    exit_fees = sum(item.fee_paid for item in legs)
    fees = position.entry_fee_paid + exit_fees
    return TradeRecord(
        order_id=position.intent.order_id,
        symbol=position.intent.symbol,
        side=position.intent.side,
        scene_family=position.intent.scene_family,
        entry_time=position.filled_at,
        entry_price=position.entry_price,
        initial_stop=position.initial_stop,
        initial_target=position.initial_target,
        original_quantity=position.original_quantity,
        target_r=position.target_r,
        exit_legs=legs,
        entry_fee_paid=position.entry_fee_paid,
        gross_pnl=gross,
        fees_paid=fees,
        net_pnl=gross - fees,
        closed_at=leg.filled_at,
        final_reason=leg.reason,
    )


def fill_initial_target(
    position: OpenPosition, *, filled_at: object, costs: CostConfig
) -> TradeRecord:
    leg = _exit_leg(
        position,
        reason="initial_target",
        filled_at=filled_at,
        fill_price=position.initial_target,
        quantity=position.remaining_quantity,
        fee_rate=costs.target_fee_rate,
    )
    return _close_position(position, leg)


def fill_stop(
    position: OpenPosition,
    *,
    filled_at: object,
    costs: CostConfig,
    actual_fill_price: float | None = None,
) -> TradeRecord:
    price = (
        _adverse_stop_fill(position.intent.side, position.stop_price, costs)
        if actual_fill_price is None
        else _positive(actual_fill_price, name="actual_fill_price")
    )
    leg = _exit_leg(
        position,
        reason="initial_stop",
        filled_at=filled_at,
        fill_price=price,
        quantity=position.remaining_quantity,
        fee_rate=costs.stop_fee_rate,
    )
    return _close_position(position, leg)


def _projected_net_after_volume_exit(
    position: OpenPosition, *, reference_price: float, costs: CostConfig
) -> tuple[float, float]:
    fill = _adverse_volume_fill(position.intent.side, reference_price, costs)
    gross = (
        _directional_distance(position.intent.side, position.entry_price, fill)
        * position.remaining_quantity
    )
    allocated_entry_fee = (
        position.entry_price
        * position.remaining_quantity
        * costs.entry_fee_rate
    )
    fees = (
        allocated_entry_fee
        + fill * position.remaining_quantity * costs.volume_exit_fee_rate
    )
    return gross - fees, fill


def schedule_profitable_volume_exit(
    position: OpenPosition,
    *,
    timeframe: Timeframe,
    completed_bar: FormationBar,
    previous_20_volumes: Sequence[float],
    costs: CostConfig,
) -> OpenPosition:
    if position.volume_exit is not None:
        return position
    if completed_bar.close_time <= position.filled_at:
        return position
    if timeframe not in {Timeframe.M5, Timeframe.M15}:
        raise ValueError("volume exit only uses 5m or 15m bars")
    if len(previous_20_volumes) != 20:
        raise ValueError("volume exit requires exactly 20 previous completed volumes")
    previous = tuple(
        _non_negative(value, name="previous volume") for value in previous_20_volumes
    )
    baseline = median(previous)
    if baseline <= 0:
        return position
    relative = completed_bar.volume / baseline
    favorable = (
        completed_bar.close >= position.entry_price
        if position.intent.side is Side.LONG
        else completed_bar.close <= position.entry_price
    )
    projected_net, _ = _projected_net_after_volume_exit(
        position, reference_price=completed_bar.close, costs=costs
    )
    if relative + 1e-12 < 2.0 or not favorable or projected_net <= 0:
        return position
    return replace(
        position,
        volume_exit=VolumeExitReservation(
            timeframe=timeframe,
            signal_bar_close=completed_bar.close_time,
            signal_close=completed_bar.close,
            relative_volume=relative,
        ),
    )


def execute_scheduled_volume_exit(
    position: OpenPosition,
    *,
    next_open_price: float,
    opened_at: object,
    costs: CostConfig,
) -> OpenPosition | TradeRecord:
    if position.volume_exit is None:
        raise ValueError("there is no scheduled volume exit")
    reference = _positive(next_open_price, name="next_open_price")
    favorable = (
        reference >= position.entry_price
        if position.intent.side is Side.LONG
        else reference <= position.entry_price
    )
    projected_net, fill = _projected_net_after_volume_exit(
        position, reference_price=reference, costs=costs
    )
    if not favorable or projected_net <= 0:
        return replace(position, volume_exit=None)
    leg = _exit_leg(
        position,
        reason="volume_spike",
        filled_at=opened_at,
        fill_price=fill,
        quantity=position.remaining_quantity,
        fee_rate=costs.volume_exit_fee_rate,
    )
    return _close_position(position, leg)


class SingleSlotRouter:
    """One system-wide slot containing one pending limit or one position."""

    def __init__(self, *, costs: CostConfig) -> None:
        self.costs = costs
        self._pending: OrderIntent | None = None
        self._position: OpenPosition | None = None
        self._trades: list[TradeRecord] = []

    @property
    def pending(self) -> OrderIntent | None:
        return self._pending

    @property
    def position(self) -> OpenPosition | None:
        return self._position

    @property
    def slot_count(self) -> int:
        return sum(item is not None for item in (self._pending, self._position))

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        return tuple(self._trades)

    def submit(self, intent: OrderIntent) -> None:
        if self.slot_count:
            raise RuntimeError("the system-wide order/position slot is occupied")
        if intent.scene_family not in {
            SceneFamily.A1_B1_CONFLUENCE,
            SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST,
            SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
            SceneFamily.OWNED_M15_ANCHOR_OVERLAP_FIRST_RETURN,
            SceneFamily.SR_FLIP_FVG,
        }:
            raise ValueError("router does not accept this scene family")
        if intent.entry_mode not in {
            EntryMode.LIMIT_FIRST_REVISIT,
            EntryMode.NEXT_BAR_OPEN,
        }:
            raise ValueError("router does not support this entry mode")
        self._pending = intent

    def cancel_entry(self) -> OrderIntent:
        intent = self._pending
        if intent is None:
            raise RuntimeError("there is no entry to cancel")
        self._pending = None
        return intent

    def fill_entry(self, *, actual_fill_price: float, filled_at: object) -> OpenPosition:
        intent = self._pending
        if intent is None or self._position is not None:
            raise RuntimeError("there is no fillable entry")
        effective_intent = _reprice_next_bar_open_intent(
            intent,
            actual_fill_price=actual_fill_price,
            costs=self.costs,
        )
        position = open_position_from_fill(
            effective_intent,
            actual_fill_price=actual_fill_price,
            filled_at=filled_at,
            costs=self.costs,
        )
        self._pending = None
        self._position = position
        return position

    def partial_fill(self, *, filled_at: object) -> OpenPosition:
        if self._position is None:
            raise RuntimeError("there is no open position")
        self._position = fill_partial_1r(
            self._position, filled_at=filled_at, costs=self.costs
        )
        return self._position

    def target_fill(self, *, filled_at: object) -> TradeRecord:
        if self._position is None:
            raise RuntimeError("there is no open position")
        record = fill_initial_target(
            self._position, filled_at=filled_at, costs=self.costs
        )
        self._position = None
        self._trades.append(record)
        return record

    def stop_fill(
        self, *, filled_at: object, actual_fill_price: float | None = None
    ) -> TradeRecord:
        if self._position is None:
            raise RuntimeError("there is no open position")
        record = fill_stop(
            self._position,
            filled_at=filled_at,
            costs=self.costs,
            actual_fill_price=actual_fill_price,
        )
        self._position = None
        self._trades.append(record)
        return record

    def schedule_volume_exit(
        self,
        *,
        timeframe: Timeframe,
        completed_bar: FormationBar,
        previous_20_volumes: Sequence[float],
    ) -> bool:
        if self._position is None:
            raise RuntimeError("there is no open position")
        updated = schedule_profitable_volume_exit(
            self._position,
            timeframe=timeframe,
            completed_bar=completed_bar,
            previous_20_volumes=previous_20_volumes,
            costs=self.costs,
        )
        scheduled = updated.volume_exit is not None and self._position.volume_exit is None
        self._position = updated
        return scheduled

    def execute_volume_exit(
        self, *, next_open_price: float, opened_at: object
    ) -> OpenPosition | TradeRecord:
        if self._position is None:
            raise RuntimeError("there is no open position")
        result = execute_scheduled_volume_exit(
            self._position,
            next_open_price=next_open_price,
            opened_at=opened_at,
            costs=self.costs,
        )
        if isinstance(result, TradeRecord):
            self._position = None
            self._trades.append(result)
        else:
            self._position = result
        return result


__all__ = [
    "CostConfig",
    "DEFAULT_DAILY_LOSS_LIMIT_ENABLED",
    "DEFAULT_DAILY_LOSS_LIMIT_FRACTION",
    "DEFAULT_DAILY_RESET_TIMEZONE",
    "DEFAULT_RISK_FRACTION",
    "ExitLeg",
    "OpenPosition",
    "OrderIntent",
    "RiskConfig",
    "SingleSlotRouter",
    "TradeRecord",
    "VolumeExitReservation",
    "build_confluence_intent",
    "build_confluence_first_revisit_intent",
    "calculate_order_quantity",
    "execute_scheduled_volume_exit",
    "fill_initial_target",
    "fill_partial_1r",
    "fill_stop",
    "open_position_from_fill",
    "schedule_profitable_volume_exit",
]

