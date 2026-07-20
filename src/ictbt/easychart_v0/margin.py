from __future__ import annotations

from dataclasses import dataclass
import math

from .domain import Side
from .execution import CostConfig
from .pipeline import Opportunity, OpportunityRejection


@dataclass(frozen=True, slots=True)
class MarginSafetyConfig:
    """Conservative isolated-margin safety model for research admission.

    This is an ENGINEERING_V0 guard, not an EasyChart source claim and not an
    exchange-tier oracle. The live adapter must replace the maintenance inputs
    with the current symbol/notional bracket before placing an order.
    """

    execution_leverage: float = 8.0
    maximum_notional_to_equity: float = 8.0
    maintenance_margin_fraction: float = 0.01
    liquidation_fee_fraction: float = 0.005
    minimum_stop_to_liquidation_r: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "execution_leverage",
            "maximum_notional_to_equity",
            "minimum_stop_to_liquidation_r",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        for name in (
            "maintenance_margin_fraction",
            "liquidation_fee_fraction",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f"{name} must be finite and in [0, 1)")
            object.__setattr__(self, name, value)
        if self.execution_leverage <= 1:
            raise ValueError("execution_leverage must exceed one")
        if self.maximum_notional_to_equity > self.execution_leverage + 1e-12:
            raise ValueError(
                "maximum_notional_to_equity cannot exceed execution leverage"
            )
        if self.maintenance_margin_fraction + self.liquidation_fee_fraction >= 1:
            raise ValueError("combined maintenance and liquidation fractions are invalid")


@dataclass(frozen=True, slots=True)
class MarginSafetyAssessment:
    accepted: bool
    reason: str | None
    required_notional_to_equity: float
    estimated_liquidation_price: float
    stop_to_liquidation_r: float


def _positive(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def required_notional_to_equity(
    *,
    side: Side,
    entry: float,
    stop: float,
    costs: CostConfig,
    risk_fraction: float,
) -> float:
    entry_price = _positive(entry, name="entry")
    stop_price = _positive(stop, name="stop")
    risk = _positive(risk_fraction, name="risk_fraction")
    if risk >= 1:
        raise ValueError("risk_fraction must be below one")
    if (side is Side.LONG and stop_price >= entry_price) or (
        side is Side.SHORT and stop_price <= entry_price
    ):
        raise ValueError("stop is on the wrong side of entry")
    fraction = costs.stop_slippage_bps / 10_000
    stop_fill = stop_price * (
        1.0 - fraction if side is Side.LONG else 1.0 + fraction
    )
    unit_risk = (
        abs(entry_price - stop_price)
        + abs(stop_price - stop_fill)
        + entry_price * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    if unit_risk <= 0:
        return math.inf
    return risk * entry_price / unit_risk


def estimate_liquidation_price(
    *,
    side: Side,
    entry: float,
    config: MarginSafetyConfig = MarginSafetyConfig(),
) -> float:
    """Approximate linear-perpetual liquidation under isolated leverage.

    Maintenance amount tiers and mark/index divergence are intentionally not
    modeled here; the conservative maintenance and liquidation fractions plus
    the stop buffer keep this an admission guard rather than a price promise.
    """

    entry_price = _positive(entry, name="entry")
    burden = config.maintenance_margin_fraction + config.liquidation_fee_fraction
    leverage = config.execution_leverage
    if side is Side.LONG:
        return entry_price * (1.0 - 1.0 / leverage) / (1.0 - burden)
    return entry_price * (1.0 + 1.0 / leverage) / (1.0 + burden)


def assess_margin_safety(
    *,
    side: Side,
    entry: float,
    stop: float,
    costs: CostConfig,
    risk_fraction: float,
    config: MarginSafetyConfig = MarginSafetyConfig(),
) -> MarginSafetyAssessment:
    exposure = required_notional_to_equity(
        side=side,
        entry=entry,
        stop=stop,
        costs=costs,
        risk_fraction=risk_fraction,
    )
    liquidation = estimate_liquidation_price(side=side, entry=entry, config=config)
    stop_distance = abs(float(entry) - float(stop))
    if stop_distance <= 0:
        raise ValueError("entry and stop cannot be equal")
    buffer = (
        float(stop) - liquidation
        if side is Side.LONG
        else liquidation - float(stop)
    )
    buffer_r = buffer / stop_distance

    reason: str | None = None
    if exposure > config.maximum_notional_to_equity + 1e-12:
        reason = "required_notional_exceeds_equity_cap"
    elif buffer <= 0:
        reason = "liquidation_precedes_structural_stop"
    elif buffer_r + 1e-12 < config.minimum_stop_to_liquidation_r:
        reason = "liquidation_buffer_below_required_r"
    return MarginSafetyAssessment(
        accepted=reason is None,
        reason=reason,
        required_notional_to_equity=exposure,
        estimated_liquidation_price=liquidation,
        stop_to_liquidation_r=buffer_r,
    )


def guard_opportunity_margin(
    opportunity: Opportunity | OpportunityRejection,
    *,
    costs: CostConfig,
    risk_fraction: float,
    config: MarginSafetyConfig = MarginSafetyConfig(),
) -> Opportunity | OpportunityRejection:
    if isinstance(opportunity, OpportunityRejection):
        return opportunity
    assessment = assess_margin_safety(
        side=opportunity.side,
        entry=opportunity.planned_entry.price,
        stop=opportunity.initial_stop,
        costs=costs,
        risk_fraction=risk_fraction,
        config=config,
    )
    if assessment.accepted:
        return opportunity
    return OpportunityRejection(
        symbol=opportunity.symbol,
        side=opportunity.side,
        authority=opportunity.authority,
        reason="target_space_conflict",
    )


__all__ = [
    "MarginSafetyAssessment",
    "MarginSafetyConfig",
    "assess_margin_safety",
    "estimate_liquidation_price",
    "guard_opportunity_margin",
    "required_notional_to_equity",
]
