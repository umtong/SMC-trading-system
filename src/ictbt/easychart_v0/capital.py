from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Sequence

import pandas as pd

SUPPORTED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"})
VenueName = Literal["BINANCE_USDM", "BYBIT_LINEAR"]
TransferAction = Literal[
    "NONE",
    "MANUAL_DEPOSIT_TO_TRADING",
    "MANUAL_WITHDRAW_TO_BANK",
    "WAIT_UNTIL_GLOBAL_SLOT_FLAT",
]


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


def _fraction(value: float, *, name: str, allow_zero: bool = False) -> float:
    number = _non_negative(value, name=name) if allow_zero else _positive(value, name=name)
    if number > 1:
        raise ValueError(f"{name} must be at most one")
    return number


def _utc(value: object, *, name: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return stamp.tz_convert("UTC")


def _floor_step(value: float, step: float) -> float:
    return math.floor((value + 1e-12) / step) * step


def validate_symbol(symbol: str) -> str:
    normalized = str(symbol).upper().strip()
    if normalized not in SUPPORTED_SYMBOLS:
        raise ValueError(
            f"unsupported symbol {normalized!r}; allowed: {sorted(SUPPORTED_SYMBOLS)}"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    """Settled total wealth; the bank balance is manually supplied and never API-controlled."""

    trading_account_equity: float
    bank_account_equity: float
    observed_at: pd.Timestamp
    bank_balance_recorded_at: pd.Timestamp

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "trading_account_equity",
            _non_negative(self.trading_account_equity, name="trading_account_equity"),
        )
        object.__setattr__(
            self,
            "bank_account_equity",
            _non_negative(self.bank_account_equity, name="bank_account_equity"),
        )
        object.__setattr__(self, "observed_at", _utc(self.observed_at, name="observed_at"))
        object.__setattr__(
            self,
            "bank_balance_recorded_at",
            _utc(self.bank_balance_recorded_at, name="bank_balance_recorded_at"),
        )
        if self.total_equity <= 0:
            raise ValueError("total equity must be positive")
        if self.bank_balance_recorded_at > self.observed_at:
            raise ValueError("bank balance cannot be recorded in the future")

    @property
    def total_equity(self) -> float:
        return self.trading_account_equity + self.bank_account_equity

    @property
    def trading_fraction(self) -> float:
        return self.trading_account_equity / self.total_equity

    @property
    def bank_fraction(self) -> float:
        return self.bank_account_equity / self.total_equity


@dataclass(frozen=True, slots=True)
class GlobalSlotState:
    """One global slot across every allowed symbol, including pending and open state."""

    pending_order_ids: tuple[str, ...] = ()
    open_position_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identities = self.pending_order_ids + self.open_position_ids
        if any(not str(item).strip() for item in identities):
            raise ValueError("slot identities must be non-empty")
        if len(set(identities)) != len(identities):
            raise ValueError("slot identities must be unique")
        if self.slot_count > 1:
            raise RuntimeError(
                "global slot invariant violated: pending orders plus open positions exceed one"
            )

    @property
    def slot_count(self) -> int:
        return len(self.pending_order_ids) + len(self.open_position_ids)

    @property
    def available(self) -> bool:
        return self.slot_count == 0

    def assert_available(self) -> None:
        if not self.available:
            raise RuntimeError("the system-wide pending/open slot is occupied")


@dataclass(frozen=True, slots=True)
class VenueLimits:
    venue: VenueName
    symbol: str
    quantity_step: float
    minimum_quantity: float
    minimum_notional: float
    maximum_notional: float
    maximum_leverage: float
    wallet_equity: float
    available_balance: float

    def __post_init__(self) -> None:
        if self.venue not in {"BINANCE_USDM", "BYBIT_LINEAR"}:
            raise ValueError("unsupported venue")
        object.__setattr__(self, "symbol", validate_symbol(self.symbol))
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
            "maximum_notional",
            _positive(self.maximum_notional, name="maximum_notional"),
        )
        if self.maximum_notional < self.minimum_notional:
            raise ValueError("maximum_notional must not be below minimum_notional")
        object.__setattr__(
            self,
            "maximum_leverage",
            _positive(self.maximum_leverage, name="maximum_leverage"),
        )
        object.__setattr__(
            self,
            "wallet_equity",
            _non_negative(self.wallet_equity, name="wallet_equity"),
        )
        object.__setattr__(
            self,
            "available_balance",
            _non_negative(self.available_balance, name="available_balance"),
        )
        if self.available_balance > self.wallet_equity + 1e-9:
            raise ValueError("available_balance cannot exceed wallet_equity")


@dataclass(frozen=True, slots=True)
class FixedLossPolicy:
    """Frozen per-trade loss policy and offline treasury constraints."""

    risk_fraction: float = 0.01
    entry_fee_rate: float = 0.0006
    stop_fee_rate: float = 0.0006
    stress_stop_slippage_bps: float = 8.0
    selected_leverage: float = 5.0
    margin_buffer_fraction: float = 0.25
    loss_buffer_multiples: float = 2.0
    minimum_bank_fraction: float = 0.35
    target_trading_fraction: float = 0.60
    rebalance_hysteresis_fraction: float = 0.05

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "risk_fraction", _fraction(self.risk_fraction, name="risk_fraction")
        )
        for name in ("entry_fee_rate", "stop_fee_rate"):
            value = _fraction(getattr(self, name), name=name, allow_zero=True)
            if value >= 1:
                raise ValueError(f"{name} must be below one")
            object.__setattr__(self, name, value)
        slippage = _non_negative(
            self.stress_stop_slippage_bps,
            name="stress_stop_slippage_bps",
        )
        if slippage >= 10_000:
            raise ValueError("stress_stop_slippage_bps must be below 10000")
        object.__setattr__(self, "stress_stop_slippage_bps", slippage)
        object.__setattr__(
            self,
            "selected_leverage",
            _positive(self.selected_leverage, name="selected_leverage"),
        )
        object.__setattr__(
            self,
            "margin_buffer_fraction",
            _non_negative(self.margin_buffer_fraction, name="margin_buffer_fraction"),
        )
        object.__setattr__(
            self,
            "loss_buffer_multiples",
            _non_negative(self.loss_buffer_multiples, name="loss_buffer_multiples"),
        )
        bank = _fraction(
            self.minimum_bank_fraction,
            name="minimum_bank_fraction",
            allow_zero=True,
        )
        target = _fraction(
            self.target_trading_fraction,
            name="target_trading_fraction",
            allow_zero=True,
        )
        if target > 1 - bank + 1e-12:
            raise ValueError("target trading fraction violates the minimum bank reserve")
        object.__setattr__(self, "minimum_bank_fraction", bank)
        object.__setattr__(self, "target_trading_fraction", target)
        hysteresis = _fraction(
            self.rebalance_hysteresis_fraction,
            name="rebalance_hysteresis_fraction",
            allow_zero=True,
        )
        object.__setattr__(self, "rebalance_hysteresis_fraction", hysteresis)


@dataclass(frozen=True, slots=True)
class FixedLossOrderSizing:
    symbol: str
    total_equity: float
    risk_fraction: float
    maximum_loss_budget: float
    configured_unit_loss: float
    quantity: float
    entry_price: float
    stop_price: float
    notional: float
    configured_stop_loss: float
    stress_stop_fill: float
    stress_unit_loss: float
    stress_stop_loss: float
    stress_budget_overrun_fraction: float
    effective_leverage: float
    buffered_initial_margin: float
    required_trading_equity: float
    maximum_trading_equity_under_bank_floor: float
    manual_deposit_required: float
    order_allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrequencyAssessment:
    completed_trades: int
    operating_days: int
    trades_per_operating_day: float
    recommendation: float
    recommendation_met: bool
    hard_gate: bool = False


@dataclass(frozen=True, slots=True)
class RebalanceInstruction:
    action: TransferAction
    amount: float
    target_trading_equity: float
    target_bank_equity: float
    manual_only: bool = True
    bank_connector_enabled: bool = False
    reason: str = ""


def configured_unit_loss(
    *,
    entry_price: float,
    stop_price: float,
    entry_fee_rate: float,
    stop_fee_rate: float,
) -> float:
    """Exact user contract: distance + entry fee + stop-price fee."""

    entry = _positive(entry_price, name="entry_price")
    stop = _positive(stop_price, name="stop_price")
    entry_fee = _fraction(entry_fee_rate, name="entry_fee_rate", allow_zero=True)
    stop_fee = _fraction(stop_fee_rate, name="stop_fee_rate", allow_zero=True)
    value = abs(entry - stop) + entry * entry_fee + stop * stop_fee
    return _positive(value, name="configured_unit_loss")


def size_fixed_loss_order(
    *,
    capital: CapitalSnapshot,
    slot: GlobalSlotState,
    limits: VenueLimits,
    policy: FixedLossPolicy,
    side: Literal["LONG", "SHORT"],
    entry_price: float,
    stop_price: float,
) -> FixedLossOrderSizing:
    """Size from total wealth, then fail closed if exchange capacity is insufficient.

    Quantity is never silently reduced to fit the exchange account. A capacity
    shortfall blocks the order and emits the amount of a manual bank-to-exchange
    deposit that would be required. The bank itself is never connected.
    """

    slot.assert_available()
    symbol = validate_symbol(limits.symbol)
    normalized_side = str(side).upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    entry = _positive(entry_price, name="entry_price")
    stop = _positive(stop_price, name="stop_price")
    if normalized_side == "LONG" and stop >= entry:
        raise ValueError("long stop must be below entry")
    if normalized_side == "SHORT" and stop <= entry:
        raise ValueError("short stop must be above entry")

    max_loss = capital.total_equity * policy.risk_fraction
    unit = configured_unit_loss(
        entry_price=entry,
        stop_price=stop,
        entry_fee_rate=policy.entry_fee_rate,
        stop_fee_rate=policy.stop_fee_rate,
    )
    quantity = _floor_step(max_loss / unit, limits.quantity_step)
    notional = quantity * entry
    configured_loss = quantity * unit

    slip = policy.stress_stop_slippage_bps / 10_000
    stress_fill = stop * (1 - slip if normalized_side == "LONG" else 1 + slip)
    stress_unit = (
        abs(entry - stress_fill)
        + entry * policy.entry_fee_rate
        + stress_fill * policy.stop_fee_rate
    )
    stress_loss = quantity * stress_unit
    overrun = 0.0 if max_loss == 0 else max(0.0, stress_loss / max_loss - 1.0)

    effective_leverage = min(policy.selected_leverage, limits.maximum_leverage)
    buffered_margin = notional / effective_leverage * (1 + policy.margin_buffer_fraction)
    required_trading_equity = buffered_margin + max_loss * policy.loss_buffer_multiples
    maximum_trading_equity = capital.total_equity * (1 - policy.minimum_bank_fraction)

    reasons: list[str] = []
    if quantity <= 0 or quantity < limits.minimum_quantity - 1e-12:
        reasons.append("quantity_below_exchange_minimum")
    if notional < limits.minimum_notional - 1e-12:
        reasons.append("notional_below_exchange_minimum")
    if notional > limits.maximum_notional + 1e-12:
        reasons.append("notional_above_current_venue_tier")
    if required_trading_equity > maximum_trading_equity + 1e-12:
        reasons.append("risk_policy_infeasible_with_minimum_bank_reserve")
    if limits.available_balance + 1e-12 < buffered_margin:
        reasons.append("available_margin_insufficient")
    if limits.wallet_equity + 1e-12 < required_trading_equity:
        reasons.append("trading_wallet_buffer_insufficient")
    if abs(capital.trading_account_equity - limits.wallet_equity) > max(
        1e-6, capital.total_equity * 0.001
    ):
        reasons.append("capital_snapshot_and_venue_wallet_mismatch")

    manual_deposit = max(
        0.0,
        buffered_margin - limits.available_balance,
        required_trading_equity - limits.wallet_equity,
    )
    if manual_deposit > capital.bank_account_equity + 1e-12:
        reasons.append("bank_reserve_cannot_cover_required_manual_deposit")

    return FixedLossOrderSizing(
        symbol=symbol,
        total_equity=capital.total_equity,
        risk_fraction=policy.risk_fraction,
        maximum_loss_budget=max_loss,
        configured_unit_loss=unit,
        quantity=quantity,
        entry_price=entry,
        stop_price=stop,
        notional=notional,
        configured_stop_loss=configured_loss,
        stress_stop_fill=stress_fill,
        stress_unit_loss=stress_unit,
        stress_stop_loss=stress_loss,
        stress_budget_overrun_fraction=overrun,
        effective_leverage=effective_leverage,
        buffered_initial_margin=buffered_margin,
        required_trading_equity=required_trading_equity,
        maximum_trading_equity_under_bank_floor=maximum_trading_equity,
        manual_deposit_required=manual_deposit,
        order_allowed=not reasons,
        reasons=tuple(reasons),
    )


def assess_trade_frequency(
    *,
    completed_trades: int,
    start: object,
    end: object,
    complete_operating_days: int | None = None,
    recommendation: float = 1.0,
) -> FrequencyAssessment:
    trades = int(completed_trades)
    if trades < 0:
        raise ValueError("completed_trades must be non-negative")
    recommended = _positive(recommendation, name="recommendation")
    if complete_operating_days is None:
        left = _utc(start, name="start").normalize()
        right = _utc(end, name="end").normalize()
        if right < left:
            raise ValueError("end must not precede start")
        days = int((right - left).days) + 1
    else:
        days = int(complete_operating_days)
        if days <= 0:
            raise ValueError("complete_operating_days must be positive")
    ratio = trades / days
    return FrequencyAssessment(
        completed_trades=trades,
        operating_days=days,
        trades_per_operating_day=ratio,
        recommendation=recommended,
        recommendation_met=ratio + 1e-12 >= recommended,
        hard_gate=False,
    )


def plan_manual_rebalance(
    *,
    capital: CapitalSnapshot,
    slot: GlobalSlotState,
    policy: FixedLossPolicy,
    required_trading_equity: float = 0.0,
) -> RebalanceInstruction:
    """Return an offline instruction only; never call a bank or transfer API."""

    required = _non_negative(required_trading_equity, name="required_trading_equity")
    total = capital.total_equity
    maximum = total * (1 - policy.minimum_bank_fraction)
    target = max(total * policy.target_trading_fraction, required)
    target = min(target, maximum)
    target_bank = total - target
    band = total * policy.rebalance_hysteresis_fraction

    if not slot.available:
        return RebalanceInstruction(
            action="WAIT_UNTIL_GLOBAL_SLOT_FLAT",
            amount=0.0,
            target_trading_equity=target,
            target_bank_equity=target_bank,
            reason="manual treasury movement is deferred while a pending order or position exists",
        )

    if capital.trading_account_equity < target - band:
        amount = min(capital.bank_account_equity, target - capital.trading_account_equity)
        return RebalanceInstruction(
            action="MANUAL_DEPOSIT_TO_TRADING",
            amount=amount,
            target_trading_equity=target,
            target_bank_equity=target_bank,
            reason="operator must transfer funds manually; the bank is not connected",
        )
    if capital.trading_account_equity > target + band:
        amount = capital.trading_account_equity - target
        return RebalanceInstruction(
            action="MANUAL_WITHDRAW_TO_BANK",
            amount=amount,
            target_trading_equity=target,
            target_bank_equity=target_bank,
            reason="operator should withdraw excess exchange capital manually",
        )
    return RebalanceInstruction(
        action="NONE",
        amount=0.0,
        target_trading_equity=target,
        target_bank_equity=target_bank,
        reason="capital is inside the no-transfer hysteresis band",
    )


def maximum_supported_risk_fraction(
    leverage_at_one_percent: Sequence[float],
    *,
    policy: FixedLossPolicy,
    venue_leverage: float | None = None,
    capacity_quantile: float = 0.99,
) -> float:
    """Capacity-only upper bound before performance or drawdown optimization."""

    if not 0 < capacity_quantile <= 1:
        raise ValueError("capacity_quantile must be in (0, 1]")
    values = sorted(
        _positive(value, name="leverage_at_one_percent")
        for value in leverage_at_one_percent
    )
    if not values:
        raise ValueError("at least one leverage observation is required")
    index = min(
        len(values) - 1,
        max(0, math.ceil(capacity_quantile * len(values)) - 1),
    )
    q = values[index]
    leverage = min(
        policy.selected_leverage,
        _positive(venue_leverage, name="venue_leverage")
        if venue_leverage is not None
        else policy.selected_leverage,
    )
    available_fraction = 1 - policy.minimum_bank_fraction
    denominator = (
        q / 0.01 / leverage * (1 + policy.margin_buffer_fraction)
        + policy.loss_buffer_multiples
    )
    return max(0.0, available_fraction / denominator)
