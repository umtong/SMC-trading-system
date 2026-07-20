from __future__ import annotations

import math

from .domain import Side
from .execution import CostConfig


def cost_inclusive_target_r(
    *,
    side: Side,
    entry_price: float,
    stop_price: float,
    target_price: float,
    costs: CostConfig,
) -> float:
    """Return target profit divided by the all-in adverse-stop loss per unit."""

    entry = float(entry_price)
    stop = float(stop_price)
    target = float(target_price)
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop, target)):
        raise ValueError("entry, stop and target prices must be finite and positive")
    if side is Side.LONG:
        if not stop < entry < target:
            raise ValueError("long geometry must satisfy stop < entry < target")
    elif not target < entry < stop:
        raise ValueError("short geometry must satisfy target < entry < stop")

    slip = costs.stop_slippage_bps / 10_000
    stop_fill = stop * (1.0 - slip if side is Side.LONG else 1.0 + slip)
    stop_price_loss = (
        entry - stop_fill if side is Side.LONG else stop_fill - entry
    )
    all_in_stop_loss = (
        stop_price_loss
        + entry * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    if all_in_stop_loss <= 0:
        raise ValueError("all-in stop loss must be positive")

    gross_target_profit = (
        target - entry if side is Side.LONG else entry - target
    )
    net_target_profit = (
        gross_target_profit
        - entry * costs.entry_fee_rate
        - target * costs.target_fee_rate
    )
    return net_target_profit / all_in_stop_loss


__all__ = ["cost_inclusive_target_r"]
