from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.capital import (
    CapitalSnapshot,
    FixedLossPolicy,
    GlobalSlotState,
    VenueLimits,
    assess_trade_frequency,
    configured_unit_loss,
    maximum_supported_risk_fraction,
    plan_manual_rebalance,
    size_fixed_loss_order,
)

NOW = pd.Timestamp("2026-07-22T00:00:00Z")


def capital(trading: float = 6_000.0, bank: float = 4_000.0) -> CapitalSnapshot:
    return CapitalSnapshot(
        trading_account_equity=trading,
        bank_account_equity=bank,
        observed_at=NOW,
        bank_balance_recorded_at=NOW,
    )


def limits(*, wallet: float = 6_000.0, available: float = 6_000.0) -> VenueLimits:
    return VenueLimits(
        venue="BINANCE_USDM",
        symbol="ETHUSDT",
        quantity_step=0.001,
        minimum_quantity=0.001,
        minimum_notional=5.0,
        maximum_notional=1_000_000.0,
        maximum_leverage=20.0,
        wallet_equity=wallet,
        available_balance=available,
    )


def policy(**updates) -> FixedLossPolicy:
    values = {
        "risk_fraction": 0.01,
        "entry_fee_rate": 0.0006,
        "stop_fee_rate": 0.0006,
        "stress_stop_slippage_bps": 8.0,
        "selected_leverage": 5.0,
        "margin_buffer_fraction": 0.25,
        "loss_buffer_multiples": 2.0,
        "minimum_bank_fraction": 0.35,
        "target_trading_fraction": 0.60,
        "rebalance_hysteresis_fraction": 0.05,
    }
    values.update(updates)
    return FixedLossPolicy(**values)


def test_exact_user_fixed_loss_formula() -> None:
    value = configured_unit_loss(
        entry_price=100.0,
        stop_price=90.0,
        entry_fee_rate=0.001,
        stop_fee_rate=0.002,
    )
    assert value == pytest.approx(10.0 + 0.1 + 0.18)


def test_quantity_uses_total_wealth_not_only_exchange_wallet() -> None:
    result = size_fixed_loss_order(
        capital=capital(),
        slot=GlobalSlotState(),
        limits=limits(),
        policy=policy(),
        side="LONG",
        entry_price=100.0,
        stop_price=90.0,
    )
    expected_unit = 10.0 + 100.0 * 0.0006 + 90.0 * 0.0006
    assert result.maximum_loss_budget == pytest.approx(100.0)
    assert result.quantity == pytest.approx(int((100.0 / expected_unit) * 1000) / 1000)
    assert result.configured_stop_loss <= result.maximum_loss_budget
    assert result.stress_stop_loss > result.configured_stop_loss


def test_unsupported_symbol_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported symbol"):
        VenueLimits(
            venue="BYBIT_LINEAR",
            symbol="DOGEUSDT",
            quantity_step=1.0,
            minimum_quantity=1.0,
            minimum_notional=5.0,
            maximum_notional=10_000.0,
            maximum_leverage=10.0,
            wallet_equity=1_000.0,
            available_balance=1_000.0,
        )


def test_global_pending_and_open_counts_cannot_exceed_one() -> None:
    with pytest.raises(RuntimeError, match="exceed one"):
        GlobalSlotState(pending_order_ids=("order-1",), open_position_ids=("position-1",))


def test_capacity_shortfall_blocks_without_reducing_quantity() -> None:
    rich_limits = limits(wallet=6_000.0, available=6_000.0)
    poor_limits = limits(wallet=500.0, available=400.0)
    kwargs = dict(
        capital=capital(trading=500.0, bank=9_500.0),
        slot=GlobalSlotState(),
        policy=policy(),
        side="LONG",
        entry_price=100.0,
        stop_price=99.0,
    )
    rich = size_fixed_loss_order(limits=rich_limits, **{**kwargs, "capital": capital()})
    poor = size_fixed_loss_order(limits=poor_limits, **kwargs)
    assert poor.quantity == rich.quantity
    assert not poor.order_allowed
    assert poor.manual_deposit_required > 0
    assert "available_margin_insufficient" in poor.reasons


def test_rebalance_is_manual_and_deferred_while_slot_occupied() -> None:
    instruction = plan_manual_rebalance(
        capital=capital(trading=4_000.0, bank=6_000.0),
        slot=GlobalSlotState(pending_order_ids=("order-1",)),
        policy=policy(),
    )
    assert instruction.action == "WAIT_UNTIL_GLOBAL_SLOT_FLAT"
    assert instruction.manual_only
    assert not instruction.bank_connector_enabled


def test_rebalance_targets_sixty_forty_with_hysteresis() -> None:
    instruction = plan_manual_rebalance(
        capital=capital(trading=4_000.0, bank=6_000.0),
        slot=GlobalSlotState(),
        policy=policy(),
    )
    assert instruction.action == "MANUAL_DEPOSIT_TO_TRADING"
    assert instruction.amount == pytest.approx(2_000.0)
    assert instruction.target_trading_equity == pytest.approx(6_000.0)
    assert instruction.target_bank_equity == pytest.approx(4_000.0)


def test_trade_frequency_is_a_soft_recommendation() -> None:
    assessment = assess_trade_frequency(
        completed_trades=30,
        start="2026-01-01T00:00:00Z",
        end="2026-03-31T23:59:59Z",
    )
    assert assessment.operating_days == 90
    assert assessment.trades_per_operating_day == pytest.approx(1 / 3)
    assert not assessment.recommendation_met
    assert not assessment.hard_gate


def test_capacity_bound_accounts_for_bank_floor_and_margin_buffer() -> None:
    bound = maximum_supported_risk_fraction(
        [1.0, 1.5, 2.0],
        policy=policy(),
        venue_leverage=5.0,
        capacity_quantile=1.0,
    )
    assert bound == pytest.approx(0.65 / 52.0)
