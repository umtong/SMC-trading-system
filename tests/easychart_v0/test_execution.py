from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import FormationBar, SceneFamily, Side, Timeframe
from ictbt.easychart_v0.execution import (
    CostConfig,
    DEFAULT_DAILY_LOSS_LIMIT_ENABLED,
    DEFAULT_RISK_FRACTION,
    OpenPosition,
    RiskConfig,
    SingleSlotRouter,
    TradeRecord,
    build_confluence_first_revisit_intent,
    calculate_order_quantity,
    execute_scheduled_volume_exit,
    fill_initial_target,
    fill_partial_1r,
    open_position_from_fill,
    schedule_profitable_volume_exit,
)


NOW = pd.Timestamp("2026-01-01T00:00:00Z")


def costs() -> CostConfig:
    return CostConfig(
        entry_fee_rate=0.001,
        stop_fee_rate=0.001,
        target_fee_rate=0.001,
        volume_exit_fee_rate=0.001,
        stop_slippage_bps=100.0,
        volume_exit_slippage_bps=10.0,
    )


def risk() -> RiskConfig:
    return RiskConfig(
        risk_fraction=0.01,
        quantity_step=0.1,
        minimum_quantity=0.1,
        minimum_notional=5.0,
    )


def test_default_user_risk_policy_is_three_percent_with_daily_limit_off() -> None:
    configured = RiskConfig()

    assert configured.risk_fraction == DEFAULT_RISK_FRACTION == 0.03
    assert configured.daily_loss_limit_enabled is DEFAULT_DAILY_LOSS_LIMIT_ENABLED is False
    assert configured.daily_loss_limit_fraction == 0.01
    assert configured.daily_reset_timezone == "Asia/Seoul"


def test_risk_config_rejects_unknown_daily_reset_timezone() -> None:
    with pytest.raises(ValueError, match="valid IANA timezone"):
        RiskConfig(daily_reset_timezone="Mars/TradingDesk")


def confluence_intent(*, target: float = 114.0, order_id: str = "confluence-order"):
    return build_confluence_first_revisit_intent(
        order_id=order_id,
        source_id="confluence-authority",
        symbol="BTCUSDT",
        side=Side.LONG,
        created_at=NOW,
        limit_price=100.0,
        initial_stop=90.0,
        initial_target=target,
        equity=10_000.0,
        costs=costs(),
        risk=risk(),
    )


def completed_bar(
    *, close: float, volume: float, opened_at: str = "2026-01-01T01:00:00Z"
) -> FormationBar:
    opened = pd.Timestamp(opened_at)
    return FormationBar(
        open_time=opened,
        close_time=opened + pd.Timedelta(minutes=5),
        open=101.0,
        high=max(103.0, close),
        low=min(99.0, close),
        close=close,
        volume=volume,
    )


def test_quantity_floors_all_in_stop_risk_to_exchange_step() -> None:
    quantity, budget, unit_risk = calculate_order_quantity(
        equity=10_000.0,
        side=Side.LONG,
        entry_reference=100.0,
        initial_stop=90.0,
        costs=costs(),
        risk=risk(),
    )

    # raw 10 + stop slippage .9 + entry fee .1 + stop-fill fee .0891
    assert unit_risk == pytest.approx(11.0891)
    assert budget == 100.0
    assert quantity == pytest.approx(9.0)
    assert quantity * unit_risk <= budget


def test_quantity_rejects_exchange_minimums() -> None:
    restrictive = RiskConfig(
        risk_fraction=0.0001,
        quantity_step=0.1,
        minimum_quantity=1.0,
        minimum_notional=100.0,
    )
    with pytest.raises(ValueError, match="minimum quantity"):
        calculate_order_quantity(
            equity=1_000.0,
            side=Side.LONG,
            entry_reference=100.0,
            initial_stop=90.0,
            costs=costs(),
            risk=restrictive,
        )


def test_only_confluence_first_revisit_limit_intent_is_built() -> None:
    intent = confluence_intent()

    assert intent.scene_family is SceneFamily.A1_B1_CONFLUENCE
    assert intent.entry_mode.value == "limit_first_revisit"
    assert intent.source_id == "confluence-authority"


def test_actual_fill_recalculates_r_and_can_remove_partial_path() -> None:
    # The planned geometry is 1.4R, but a worse actual fill leaves only 13/11R.
    position = open_position_from_fill(
        confluence_intent(), actual_fill_price=101.0, filled_at=NOW, costs=costs()
    )

    assert position.r_price == 11.0
    assert position.target_r == pytest.approx(13.0 / 11.0)
    assert position.partial_price is None
    with pytest.raises(ValueError, match="does not use the partial path"):
        fill_partial_1r(position, filled_at=NOW + pd.Timedelta(minutes=5), costs=costs())


def test_exact_1_4r_uses_one_r_half_then_entry_stop_and_initial_target() -> None:
    position = open_position_from_fill(
        confluence_intent(), actual_fill_price=100.0, filled_at=NOW, costs=costs()
    )

    assert position.target_r == pytest.approx(1.4)
    assert position.partial_price == 110.0
    after_partial = fill_partial_1r(
        position, filled_at=NOW + pd.Timedelta(minutes=5), costs=costs()
    )
    assert after_partial.partial_filled
    assert after_partial.remaining_quantity == pytest.approx(position.original_quantity / 2)
    assert after_partial.stop_price == position.entry_price
    assert after_partial.initial_target == 114.0

    trade = fill_initial_target(
        after_partial, filled_at=NOW + pd.Timedelta(minutes=10), costs=costs()
    )
    assert [leg.reason for leg in trade.exit_legs] == ["partial_1r", "initial_target"]
    assert sum(leg.quantity for leg in trade.exit_legs) == pytest.approx(
        trade.original_quantity
    )
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees_paid)


def test_below_1_4r_has_one_full_initial_target_exit() -> None:
    position = open_position_from_fill(
        confluence_intent(target=113.9), actual_fill_price=100.0, filled_at=NOW, costs=costs()
    )
    assert position.partial_price is None

    trade = fill_initial_target(
        position, filled_at=NOW + pd.Timedelta(minutes=10), costs=costs()
    )
    assert len(trade.exit_legs) == 1
    assert trade.exit_legs[0].reason == "initial_target"
    assert trade.exit_legs[0].quantity == trade.original_quantity


def test_volume_exit_schedules_on_completed_bar_and_cancels_bad_next_open() -> None:
    position = open_position_from_fill(
        confluence_intent(), actual_fill_price=100.0, filled_at=NOW, costs=costs()
    )
    signaled = schedule_profitable_volume_exit(
        position,
        timeframe=Timeframe.M5,
        completed_bar=completed_bar(close=102.0, volume=200.0),
        previous_20_volumes=[100.0] * 20,
        costs=costs(),
    )
    assert signaled.volume_exit is not None
    assert signaled.volume_exit.relative_volume == 2.0

    continued = execute_scheduled_volume_exit(
        signaled,
        next_open_price=99.0,
        opened_at=pd.Timestamp("2026-01-01T01:05:00Z"),
        costs=costs(),
    )
    assert isinstance(continued, OpenPosition)
    assert continued.volume_exit is None
    assert continued.stop_price == position.stop_price
    assert continued.initial_target == position.initial_target


def test_volume_exit_rechecks_profit_and_closes_all_at_next_open() -> None:
    position = open_position_from_fill(
        confluence_intent(), actual_fill_price=100.0, filled_at=NOW, costs=costs()
    )
    signaled = schedule_profitable_volume_exit(
        position,
        timeframe=Timeframe.M15,
        completed_bar=completed_bar(close=102.0, volume=250.0),
        previous_20_volumes=[100.0] * 20,
        costs=costs(),
    )
    result = execute_scheduled_volume_exit(
        signaled,
        next_open_price=103.0,
        opened_at=pd.Timestamp("2026-01-01T01:05:00Z"),
        costs=costs(),
    )

    assert isinstance(result, TradeRecord)
    assert result.final_reason == "volume_spike"
    assert result.exit_legs[-1].quantity == result.original_quantity
    assert result.net_pnl > 0


def test_volume_exit_profit_check_uses_remaining_position_not_realized_partial() -> None:
    position = open_position_from_fill(
        confluence_intent(), actual_fill_price=100.0, filled_at=NOW, costs=costs()
    )
    after_partial = fill_partial_1r(
        position, filled_at=NOW + pd.Timedelta(minutes=5), costs=costs()
    )
    # The completed trade is profitable because of the prior partial, but the
    # remaining half would lose money if exited here. It must not schedule.
    unchanged = schedule_profitable_volume_exit(
        after_partial,
        timeframe=Timeframe.M5,
        completed_bar=completed_bar(close=100.05, volume=250.0),
        previous_20_volumes=[100.0] * 20,
        costs=costs(),
    )
    assert unchanged.volume_exit is None


def test_router_enforces_one_pending_or_open_confluence_entry() -> None:
    router = SingleSlotRouter(costs=costs())
    first = confluence_intent(order_id="first")
    second = confluence_intent(order_id="second")

    router.submit(first)
    assert router.pending is first
    assert router.slot_count == 1
    with pytest.raises(RuntimeError, match="slot is occupied"):
        router.submit(second)

    router.fill_entry(actual_fill_price=100.0, filled_at=NOW)
    assert router.position is not None
    assert router.pending is None
    assert router.slot_count == 1
    with pytest.raises(RuntimeError, match="slot is occupied"):
        router.submit(second)

    router.target_fill(filled_at=NOW + pd.Timedelta(minutes=10))
    assert router.slot_count == 0
    router.submit(second)
    assert router.pending is second
    assert router.slot_count == 1
