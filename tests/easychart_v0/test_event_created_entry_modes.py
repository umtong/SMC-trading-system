from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import EntryMode, OBCausalState, Side
from ictbt.easychart_v0.execution import (
    CostConfig,
    RiskConfig,
    build_confluence_first_revisit_intent,
    build_confluence_intent,
)
from ictbt.easychart_v0.replay import replay_intent


NOW = pd.Timestamp("2026-01-01T00:00:00Z")


def costs() -> CostConfig:
    return CostConfig(
        entry_fee_rate=0.0,
        stop_fee_rate=0.0,
        target_fee_rate=0.0,
        volume_exit_fee_rate=0.0,
    )


def frame(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        index=pd.date_range(NOW, periods=len(rows), freq="5min"),
        columns=["open", "high", "low", "close", "volume"],
    )


def event_created_intent(
    entry_mode: EntryMode,
    *,
    risk: RiskConfig | None = None,
):
    return build_confluence_intent(
        order_id=f"event-created:{entry_mode.value}",
        source_id="m15-event:m5-delivery-ob",
        symbol="BTCUSDT",
        side=Side.LONG,
        created_at=NOW,
        entry_reference=100.0,
        initial_stop=90.0,
        initial_target=130.0,
        equity=10_000.0,
        costs=costs(),
        risk=risk or RiskConfig(risk_fraction=0.01, quantity_step=0.1),
        entry_mode=entry_mode,
        ob_causal_state=OBCausalState.EVENT_CREATED,
    )


def test_preexisting_ob_preserves_first_revisit_limit_contract() -> None:
    intent = build_confluence_first_revisit_intent(
        order_id="preexisting",
        source_id="preexisting-ob",
        symbol="BTCUSDT",
        side=Side.LONG,
        created_at=NOW,
        limit_price=100.0,
        initial_stop=90.0,
        initial_target=130.0,
        equity=10_000.0,
        costs=costs(),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.1),
    )

    assert intent.entry_mode is EntryMode.LIMIT_FIRST_REVISIT
    assert intent.ob_causal_state is OBCausalState.PREEXISTING


def test_next_bar_open_is_rejected_for_a_preexisting_ob() -> None:
    with pytest.raises(ValueError, match="event-created OB"):
        build_confluence_intent(
            order_id="invalid-blanket-open",
            source_id="preexisting-ob",
            symbol="BTCUSDT",
            side=Side.LONG,
            created_at=NOW,
            entry_reference=100.0,
            initial_stop=90.0,
            initial_target=130.0,
            equity=10_000.0,
            costs=costs(),
            risk=RiskConfig(risk_fraction=0.01, quantity_step=0.1),
            entry_mode=EntryMode.NEXT_BAR_OPEN,
            ob_causal_state=OBCausalState.PREEXISTING,
        )


def test_event_created_first_revisit_waits_for_the_ob_body() -> None:
    result = replay_intent(
        event_created_intent(EntryMode.LIMIT_FIRST_REVISIT),
        candles=frame(
            [
                (105.0, 109.0, 101.0, 106.0, 100.0),
                (104.0, 105.0, 99.0, 102.0, 100.0),
            ]
        ),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "OPEN_CENSORED"
    assert result.open_position is not None
    assert result.open_position.entry_price == 100.0
    assert result.open_position.filled_at == NOW + pd.Timedelta(minutes=10)


def test_event_created_next_open_fills_without_waiting_for_ob_revisit() -> None:
    result = replay_intent(
        event_created_intent(EntryMode.NEXT_BAR_OPEN),
        candles=frame([(105.0, 109.0, 103.0, 106.0, 100.0)]),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "OPEN_CENSORED"
    assert result.open_position is not None
    assert result.open_position.entry_price == 105.0
    assert result.open_position.filled_at == NOW
    assert [event.kind for event in result.events] == ["entry_filled", "open_censored"]


def test_adverse_next_open_gap_resizes_quantity_to_the_same_cash_risk() -> None:
    submitted = event_created_intent(EntryMode.NEXT_BAR_OPEN)
    assert submitted.quantity == 10.0

    result = replay_intent(
        submitted,
        candles=frame([(110.0, 112.0, 109.0, 111.0, 100.0)]),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.open_position is not None
    effective = result.open_position.intent
    assert effective.entry_reference == 110.0
    assert effective.unit_stop_risk == 20.0
    assert effective.quantity == 5.0
    assert effective.quantity * effective.unit_stop_risk <= effective.risk_budget
    assert result.intent is submitted


def test_next_open_below_actual_exchange_minimum_is_cleanly_rejected() -> None:
    intent = event_created_intent(
        EntryMode.NEXT_BAR_OPEN,
        risk=RiskConfig(
            risk_fraction=0.01,
            quantity_step=0.1,
            minimum_quantity=8.0,
        ),
    )
    result = replay_intent(
        intent,
        candles=frame([(110.0, 112.0, 109.0, 111.0, 100.0)]),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "ENTRY_REJECTED"
    assert result.rejection_reason is not None
    assert "below the minimum quantity" in result.rejection_reason
    assert [event.kind for event in result.events] == ["entry_rejected"]


@pytest.mark.parametrize("open_price", [90.0, 130.0])
def test_next_open_at_stop_or_target_is_cleanly_rejected(open_price: float) -> None:
    result = replay_intent(
        event_created_intent(EntryMode.NEXT_BAR_OPEN),
        candles=frame([(open_price, open_price, open_price, open_price, 100.0)]),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "ENTRY_REJECTED"
    assert result.rejection_reason == "next_bar_open_outside_trade_geometry"
    assert [event.kind for event in result.events] == ["entry_rejected"]