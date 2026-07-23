from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable

import pandas as pd

from .domain import Side
from .execution import CostConfig, TradeRecord


class EntryLiquidity(str, Enum):
    MAKER_LIMIT = "maker_limit"
    TAKER_MARKET = "taker_market"


@dataclass(frozen=True, slots=True)
class ExecutionCostConfig:
    """Entry-mode-aware linear costs used by research, paper and live adapters."""

    maker_entry_fee_rate: float
    taker_entry_fee_rate: float
    stop_fee_rate: float
    target_fee_rate: float
    volume_exit_fee_rate: float
    market_entry_slippage_bps: float = 0.0
    stop_slippage_bps: float = 0.0
    volume_exit_slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "maker_entry_fee_rate",
            "taker_entry_fee_rate",
            "stop_fee_rate",
            "target_fee_rate",
            "volume_exit_fee_rate",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0 or value >= 1:
                raise ValueError(f"{name} must be finite in [0, 1)")
            object.__setattr__(self, name, value)
        for name in (
            "market_entry_slippage_bps",
            "stop_slippage_bps",
            "volume_exit_slippage_bps",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0 or value >= 10_000:
                raise ValueError(f"{name} must be finite in [0, 10000)")
            object.__setattr__(self, name, value)

    def entry_fee_rate(self, liquidity: EntryLiquidity | str) -> float:
        selected = EntryLiquidity(liquidity)
        return (
            self.maker_entry_fee_rate
            if selected is EntryLiquidity.MAKER_LIMIT
            else self.taker_entry_fee_rate
        )

    def legacy(self, liquidity: EntryLiquidity | str) -> CostConfig:
        """Build a compatible exit engine config with the selected entry fee."""

        return CostConfig(
            entry_fee_rate=self.entry_fee_rate(liquidity),
            stop_fee_rate=self.stop_fee_rate,
            target_fee_rate=self.target_fee_rate,
            volume_exit_fee_rate=self.volume_exit_fee_rate,
            stop_slippage_bps=self.stop_slippage_bps,
            volume_exit_slippage_bps=self.volume_exit_slippage_bps,
        )


@dataclass(frozen=True, slots=True)
class ExecutionRiskConfig:
    risk_fraction: float = 0.03
    quantity_step: float = 0.001
    minimum_quantity: float = 0.0
    minimum_notional: float = 0.0
    maximum_notional_to_equity: float = 10.0

    def __post_init__(self) -> None:
        for name in ("risk_fraction", "quantity_step", "maximum_notional_to_equity"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if self.risk_fraction > 1:
            raise ValueError("risk_fraction must be at most one")
        for name in ("minimum_quantity", "minimum_notional"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class SizingDecision:
    account_equity: float
    side: Side
    entry_liquidity: EntryLiquidity
    reference_entry_price: float
    modeled_entry_price: float
    adverse_stop_fill_price: float
    risk_budget: float
    unit_stop_risk: float
    risk_limited_quantity: float
    notional_cap_quantity: float
    quantity: float
    position_notional: float
    notional_to_equity: float
    notional_cap_binding: bool


@dataclass(frozen=True, slots=True)
class FundingObservation:
    symbol: str
    funding_time: pd.Timestamp
    funding_rate: float
    mark_price: float

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("funding symbol is required")
        timestamp = pd.Timestamp(self.funding_time)
        if pd.isna(timestamp) or timestamp.tz is None:
            raise ValueError("funding_time must be timezone-aware")
        rate = float(self.funding_rate)
        mark = float(self.mark_price)
        if not math.isfinite(rate):
            raise ValueError("funding_rate must be finite")
        if not math.isfinite(mark) or mark <= 0:
            raise ValueError("mark_price must be finite and positive")
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "funding_time", timestamp.tz_convert("UTC"))
        object.__setattr__(self, "funding_rate", rate)
        object.__setattr__(self, "mark_price", mark)


@dataclass(frozen=True, slots=True)
class FundingLeg:
    funding_time: pd.Timestamp
    funding_rate: float
    mark_price: float
    quantity: float
    cash_flow: float


@dataclass(frozen=True, slots=True)
class FundedTrade:
    trade: TradeRecord
    funding_legs: tuple[FundingLeg, ...]
    funding_cash_flow: float
    net_pnl_after_funding: float


def _positive(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _floor_step(value: float, step: float) -> float:
    return math.floor((value + 1e-12) / step) * step


def _adverse_price(side: Side, reference: float, bps: float) -> float:
    fraction = bps / 10_000.0
    return reference * (1.0 + fraction if side is Side.LONG else 1.0 - fraction)


def modeled_entry_price(
    *,
    side: Side,
    reference_price: float,
    liquidity: EntryLiquidity | str,
    costs: ExecutionCostConfig,
) -> float:
    reference = _positive(reference_price, name="reference_price")
    selected = EntryLiquidity(liquidity)
    if selected is EntryLiquidity.MAKER_LIMIT:
        return reference
    return _adverse_price(side, reference, costs.market_entry_slippage_bps)


def size_position(
    *,
    equity: float,
    side: Side,
    reference_entry_price: float,
    initial_stop: float,
    liquidity: EntryLiquidity | str,
    costs: ExecutionCostConfig,
    risk: ExecutionRiskConfig,
) -> SizingDecision:
    """Apply cash-risk sizing and a hard notional/equity cap in one function."""

    account_equity = _positive(equity, name="equity")
    stop = _positive(initial_stop, name="initial_stop")
    selected = EntryLiquidity(liquidity)
    entry = modeled_entry_price(
        side=side,
        reference_price=reference_entry_price,
        liquidity=selected,
        costs=costs,
    )
    if (side is Side.LONG and stop >= entry) or (side is Side.SHORT and stop <= entry):
        raise ValueError("initial stop is on the wrong side of modeled entry")
    adverse_stop = _adverse_price(side, stop, -costs.stop_slippage_bps)
    # For a long stop sell, adverse execution is below the stop. For a short
    # stop buy, adverse execution is above it. _adverse_price with negative bps
    # has exactly that geometry for the side convention above.
    if side is Side.SHORT:
        adverse_stop = stop * (1.0 + costs.stop_slippage_bps / 10_000.0)
    else:
        adverse_stop = stop * (1.0 - costs.stop_slippage_bps / 10_000.0)

    unit_risk = (
        abs(entry - stop)
        + abs(stop - adverse_stop)
        + entry * costs.entry_fee_rate(selected)
        + adverse_stop * costs.stop_fee_rate
    )
    if not math.isfinite(unit_risk) or unit_risk <= 0:
        raise ValueError("unit stop risk must be finite and positive")
    budget = account_equity * risk.risk_fraction
    risk_quantity_raw = budget / unit_risk
    cap_quantity_raw = (
        account_equity * risk.maximum_notional_to_equity / entry
    )
    quantity = _floor_step(
        min(risk_quantity_raw, cap_quantity_raw),
        risk.quantity_step,
    )
    if quantity <= 0 or quantity < risk.minimum_quantity - 1e-12:
        raise ValueError("sized quantity is below the minimum quantity")
    notional = quantity * entry
    if notional < risk.minimum_notional - 1e-12:
        raise ValueError("sized quantity is below the minimum notional")
    ratio = notional / account_equity
    if ratio > risk.maximum_notional_to_equity + 1e-12:
        raise AssertionError("notional cap was not enforced after step rounding")
    return SizingDecision(
        account_equity=account_equity,
        side=side,
        entry_liquidity=selected,
        reference_entry_price=float(reference_entry_price),
        modeled_entry_price=entry,
        adverse_stop_fill_price=adverse_stop,
        risk_budget=budget,
        unit_stop_risk=unit_risk,
        risk_limited_quantity=_floor_step(risk_quantity_raw, risk.quantity_step),
        notional_cap_quantity=_floor_step(cap_quantity_raw, risk.quantity_step),
        quantity=quantity,
        position_notional=notional,
        notional_to_equity=ratio,
        notional_cap_binding=cap_quantity_raw + 1e-12 < risk_quantity_raw,
    )


def _remaining_quantity_before(
    trade: TradeRecord,
    timestamp: pd.Timestamp,
) -> float:
    exited = sum(
        leg.quantity
        for leg in trade.exit_legs
        if pd.Timestamp(leg.filled_at).tz_convert("UTC") < timestamp
    )
    return max(0.0, trade.original_quantity - exited)


def funding_legs_for_trade(
    trade: TradeRecord,
    observations: Iterable[FundingObservation],
) -> tuple[FundingLeg, ...]:
    """Calculate funding on actual remaining quantity at each settlement.

    At an identical settlement and exit timestamp the position-before-exit
    quantity is used. This deterministic ordering is conservative for adverse
    funding and must match the live reconciliation event clock.
    """

    entry_time = pd.Timestamp(trade.entry_time).tz_convert("UTC")
    closed_at = pd.Timestamp(trade.closed_at).tz_convert("UTC")
    side_sign = 1.0 if trade.side is Side.LONG else -1.0
    selected = sorted(
        (
            item
            for item in observations
            if item.symbol == trade.symbol.upper()
            and entry_time <= item.funding_time <= closed_at
        ),
        key=lambda item: item.funding_time,
    )
    if len({item.funding_time for item in selected}) != len(selected):
        raise ValueError("funding observations contain duplicate settlement times")

    legs: list[FundingLeg] = []
    for observation in selected:
        quantity = _remaining_quantity_before(trade, observation.funding_time)
        if quantity <= 0:
            continue
        cash = (
            -side_sign
            * quantity
            * observation.mark_price
            * observation.funding_rate
        )
        legs.append(
            FundingLeg(
                funding_time=observation.funding_time,
                funding_rate=observation.funding_rate,
                mark_price=observation.mark_price,
                quantity=quantity,
                cash_flow=cash,
            )
        )
    return tuple(legs)


def apply_funding(
    trade: TradeRecord,
    observations: Iterable[FundingObservation],
) -> FundedTrade:
    legs = funding_legs_for_trade(trade, observations)
    cash = math.fsum(item.cash_flow for item in legs)
    return FundedTrade(
        trade=trade,
        funding_legs=legs,
        funding_cash_flow=cash,
        net_pnl_after_funding=trade.net_pnl + cash,
    )


__all__ = [
    "EntryLiquidity",
    "ExecutionCostConfig",
    "ExecutionRiskConfig",
    "FundedTrade",
    "FundingLeg",
    "FundingObservation",
    "SizingDecision",
    "apply_funding",
    "funding_legs_for_trade",
    "modeled_entry_price",
    "size_position",
]
