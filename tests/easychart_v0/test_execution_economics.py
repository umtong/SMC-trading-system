from __future__ import annotations

import math

from ictbt.easychart_v0.domain import Side
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.execution_economics import cost_inclusive_target_r


def costs() -> CostConfig:
    return CostConfig(
        entry_fee_rate=0.0002,
        stop_fee_rate=0.0006,
        target_fee_rate=0.0002,
        volume_exit_fee_rate=0.0006,
        stop_slippage_bps=2.0,
        volume_exit_slippage_bps=2.0,
    )


def test_cost_inclusive_target_r_uses_the_same_adverse_stop_budget() -> None:
    result = cost_inclusive_target_r(
        side=Side.LONG,
        entry_price=100.0,
        stop_price=99.0,
        target_price=101.0,
        costs=costs(),
    )

    stop_fill = 99.0 * (1.0 - 2.0 / 10_000)
    all_in_loss = 100.0 - stop_fill + 100.0 * 0.0002 + stop_fill * 0.0006
    net_profit = 1.0 - 100.0 * 0.0002 - 101.0 * 0.0002
    assert math.isclose(result, net_profit / all_in_loss, rel_tol=1e-12)


def test_cost_inclusive_target_r_rejects_invalid_geometry() -> None:
    try:
        cost_inclusive_target_r(
            side=Side.SHORT,
            entry_price=100.0,
            stop_price=99.0,
            target_price=98.0,
            costs=costs(),
        )
    except ValueError as exc:
        assert "short geometry" in str(exc)
    else:
        raise AssertionError("invalid short geometry must be rejected")
