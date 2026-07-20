from __future__ import annotations

import pytest

from ictbt.easychart_v0.domain import Side
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.margin import (
    MarginSafetyConfig,
    assess_margin_safety,
    estimate_liquidation_price,
    required_notional_to_equity,
)


def costs() -> CostConfig:
    return CostConfig(
        entry_fee_rate=0.0002,
        stop_fee_rate=0.0006,
        target_fee_rate=0.0002,
        volume_exit_fee_rate=0.0006,
        stop_slippage_bps=2.0,
        volume_exit_slippage_bps=2.0,
    )


def test_liquidation_estimate_is_beyond_a_normal_structural_stop() -> None:
    config = MarginSafetyConfig()
    long_liq = estimate_liquidation_price(
        side=Side.LONG,
        entry=100.0,
        config=config,
    )
    short_liq = estimate_liquidation_price(
        side=Side.SHORT,
        entry=100.0,
        config=config,
    )

    assert long_liq < 90.0
    assert short_liq > 110.0
    assert assess_margin_safety(
        side=Side.LONG,
        entry=100.0,
        stop=98.0,
        costs=costs(),
        risk_fraction=0.03,
        config=config,
    ).accepted
    assert assess_margin_safety(
        side=Side.SHORT,
        entry=100.0,
        stop=102.0,
        costs=costs(),
        risk_fraction=0.03,
        config=config,
    ).accepted


def test_tight_stop_that_requires_excessive_notional_is_rejected() -> None:
    assessment = assess_margin_safety(
        side=Side.LONG,
        entry=100.0,
        stop=99.8,
        costs=costs(),
        risk_fraction=0.03,
    )

    assert not assessment.accepted
    assert assessment.reason == "required_notional_exceeds_equity_cap"
    assert assessment.required_notional_to_equity > 8.0


def test_required_exposure_is_equity_scale_invariant() -> None:
    ratio = required_notional_to_equity(
        side=Side.LONG,
        entry=100.0,
        stop=99.0,
        costs=costs(),
        risk_fraction=0.03,
    )

    assert ratio == pytest.approx(2.7293, abs=1e-3)
