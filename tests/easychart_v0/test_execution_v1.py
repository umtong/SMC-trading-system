from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import SceneFamily, Side
from ictbt.easychart_v0.execution import ExitLeg, TradeRecord
from ictbt.easychart_v0.execution_v1 import (
    EntryLiquidity,
    ExecutionCostConfig,
    ExecutionRiskConfig,
    FundingObservation,
    apply_funding,
    modeled_entry_price,
    size_position,
)


T0 = pd.Timestamp("2026-01-01T00:00:00Z")


def costs() -> ExecutionCostConfig:
    return ExecutionCostConfig(
        maker_entry_fee_rate=0.0002,
        taker_entry_fee_rate=0.0006,
        stop_fee_rate=0.0006,
        target_fee_rate=0.0002,
        volume_exit_fee_rate=0.0006,
        market_entry_slippage_bps=1.0,
        stop_slippage_bps=2.0,
        volume_exit_slippage_bps=2.0,
    )


def risk(*, cap: float = 10.0) -> ExecutionRiskConfig:
    return ExecutionRiskConfig(
        risk_fraction=0.03,
        quantity_step=0.001,
        maximum_notional_to_equity=cap,
    )


def test_market_entry_uses_taker_slippage_and_fee() -> None:
    long_price = modeled_entry_price(
        side=Side.LONG,
        reference_price=100.0,
        liquidity=EntryLiquidity.TAKER_MARKET,
        costs=costs(),
    )
    short_price = modeled_entry_price(
        side=Side.SHORT,
        reference_price=100.0,
        liquidity="taker_market",
        costs=costs(),
    )
    limit_price = modeled_entry_price(
        side=Side.LONG,
        reference_price=100.0,
        liquidity="maker_limit",
        costs=costs(),
    )

    assert long_price == pytest.approx(100.01)
    assert short_price == pytest.approx(99.99)
    assert limit_price == pytest.approx(100.0)
    assert costs().entry_fee_rate("taker_market") == pytest.approx(0.0006)
    assert costs().entry_fee_rate("maker_limit") == pytest.approx(0.0002)


def test_narrow_stop_is_capped_by_notional_to_equity() -> None:
    decision = size_position(
        equity=10_000.0,
        side=Side.LONG,
        reference_entry_price=100.0,
        initial_stop=99.9,
        liquidity="taker_market",
        costs=costs(),
        risk=risk(cap=10.0),
    )

    assert decision.notional_cap_binding
    assert decision.quantity <= decision.notional_cap_quantity + 1e-12
    assert decision.position_notional <= 100_000.0 + 1e-9
    assert decision.notional_to_equity <= 10.0 + 1e-12
    assert decision.risk_budget == pytest.approx(300.0)


def test_wide_stop_remains_cash_risk_limited() -> None:
    decision = size_position(
        equity=10_000.0,
        side=Side.LONG,
        reference_entry_price=100.0,
        initial_stop=90.0,
        liquidity="maker_limit",
        costs=costs(),
        risk=risk(cap=10.0),
    )

    assert not decision.notional_cap_binding
    assert decision.quantity == decision.risk_limited_quantity
    assert decision.quantity * decision.unit_stop_risk <= decision.risk_budget


def trade(side: Side) -> TradeRecord:
    partial = ExitLeg(
        reason="partial_1r",
        filled_at=T0 + pd.Timedelta(hours=4),
        fill_price=110.0 if side is Side.LONG else 90.0,
        quantity=5.0,
        gross_pnl=50.0,
        fee_paid=0.1,
    )
    target = ExitLeg(
        reason="initial_target",
        filled_at=T0 + pd.Timedelta(hours=12),
        fill_price=120.0 if side is Side.LONG else 80.0,
        quantity=5.0,
        gross_pnl=100.0,
        fee_paid=0.1,
    )
    return TradeRecord(
        order_id="trade",
        symbol="BTCUSDT",
        side=side,
        scene_family=SceneFamily.SR_FLIP_FVG,
        entry_time=T0,
        entry_price=100.0,
        initial_stop=90.0 if side is Side.LONG else 110.0,
        initial_target=120.0 if side is Side.LONG else 80.0,
        original_quantity=10.0,
        target_r=2.0,
        exit_legs=(partial, target),
        entry_fee_paid=0.2,
        gross_pnl=150.0,
        fees_paid=0.4,
        net_pnl=149.6,
        closed_at=target.filled_at,
        final_reason="initial_target",
    )


def observations() -> tuple[FundingObservation, ...]:
    return (
        FundingObservation(
            "BTCUSDT",
            T0 + pd.Timedelta(hours=2),
            0.001,
            100.0,
        ),
        FundingObservation(
            "BTCUSDT",
            T0 + pd.Timedelta(hours=8),
            0.001,
            110.0,
        ),
    )


def test_long_pays_positive_funding_on_actual_remaining_quantity() -> None:
    funded = apply_funding(trade(Side.LONG), observations())

    assert [leg.quantity for leg in funded.funding_legs] == [10.0, 5.0]
    assert funded.funding_legs[0].cash_flow == pytest.approx(-1.0)
    assert funded.funding_legs[1].cash_flow == pytest.approx(-0.55)
    assert funded.funding_cash_flow == pytest.approx(-1.55)
    assert funded.net_pnl_after_funding == pytest.approx(148.05)


def test_short_receives_positive_funding() -> None:
    funded = apply_funding(trade(Side.SHORT), observations())

    assert funded.funding_cash_flow == pytest.approx(1.55)
    assert funded.net_pnl_after_funding == pytest.approx(151.15)


def test_funding_validation_rejects_missing_mark_or_duplicate_settlement() -> None:
    with pytest.raises(ValueError, match="mark_price"):
        FundingObservation("BTCUSDT", T0, 0.001, float("nan"))

    duplicate = FundingObservation("BTCUSDT", T0 + pd.Timedelta(hours=2), 0.001, 100.0)
    with pytest.raises(ValueError, match="duplicate"):
        apply_funding(trade(Side.LONG), (duplicate, duplicate))
