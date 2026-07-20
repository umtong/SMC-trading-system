from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import Side
from ictbt.easychart_v0.execution import (
    CostConfig,
    RiskConfig,
    build_confluence_first_revisit_intent,
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


def risk() -> RiskConfig:
    return RiskConfig(risk_fraction=0.01, quantity_step=0.1)


def confluence(side: Side):
    if side is Side.LONG:
        stop, target = 90.0, 114.0
    else:
        stop, target = 110.0, 86.0
    return build_confluence_first_revisit_intent(
        order_id=f"confluence-{side.value}",
        source_id="confluence-authority",
        symbol="BTCUSDT",
        side=side,
        created_at=NOW,
        limit_price=100.0,
        initial_stop=stop,
        initial_target=target,
        equity=10_000.0,
        costs=costs(),
        risk=risk(),
    )
def frame(
    rows: list[tuple[float, float, float, float, float]], *, freq: str = "5min"
) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        index=pd.date_range(NOW, periods=len(rows), freq=freq),
        columns=["open", "high", "low", "close", "volume"],
    )


def event_kinds(result) -> list[str]:
    return [event.kind for event in result.events]


def test_confluence_long_first_revisit_then_partial_and_target_execute_in_order() -> None:
    candles = frame(
        [
            (99.0, 105.0, 98.0, 104.0, 100.0),
            (105.0, 114.0, 101.0, 113.0, 100.0),
        ]
    )

    result = replay_intent(
        confluence(Side.LONG),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "CLOSED"
    assert result.trade is not None
    assert result.trade.entry_price == 99.0
    assert [leg.reason for leg in result.trade.exit_legs] == [
        "partial_1r",
        "initial_target",
    ]
    assert event_kinds(result) == ["entry_filled", "partial_1r", "initial_target"]


def test_confluence_short_limit_touch_and_stop_same_bar_records_entry_then_stop() -> None:
    candles = frame([(95.0, 111.0, 94.0, 105.0, 100.0)])

    result = replay_intent(
        confluence(Side.SHORT),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "CLOSED"
    assert result.trade is not None
    assert result.trade.entry_price == 100.0
    assert result.trade.final_reason == "initial_stop"
    assert event_kinds(result) == ["entry_filled", "initial_stop"]


def test_intrabar_entry_does_not_reuse_an_earlier_same_bar_target_touch() -> None:
    candles = frame([(105.0, 115.0, 99.0, 112.0, 100.0)])

    result = replay_intent(
        confluence(Side.LONG),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "OPEN_CENSORED"
    assert result.open_position is not None
    assert result.open_position.filled_at == NOW + pd.Timedelta(minutes=5)
    assert event_kinds(result) == ["entry_filled", "open_censored"]


def test_waits_for_first_revisit_and_never_adds_on_later_revisits() -> None:
    candles = frame(
        [
            (105.0, 108.0, 101.0, 104.0, 100.0),
            (104.0, 105.0, 100.0, 103.0, 100.0),
            (103.0, 104.0, 99.0, 102.0, 100.0),
        ]
    )

    result = replay_intent(
        confluence(Side.LONG),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "OPEN_CENSORED"
    assert result.open_position is not None
    assert result.open_position.entry_price == 100.0
    assert result.open_position.remaining_quantity == result.open_position.original_quantity
    assert event_kinds(result).count("entry_filled") == 1


def test_smallest_bar_stop_first_is_symmetric_for_an_open_short() -> None:
    candles = frame([(100.0, 111.0, 85.0, 100.0, 100.0)])

    result = replay_intent(
        confluence(Side.SHORT),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "CLOSED"
    assert result.trade is not None
    assert result.trade.final_reason == "initial_stop"
    assert event_kinds(result) == ["entry_filled", "initial_stop"]


def test_lower_native_bars_resolve_a_wide_bar_into_partial_then_target() -> None:
    wide = frame([(100.0, 114.0, 90.0, 110.0, 500.0)])
    lower = frame(
        [
            (100.0, 105.0, 99.0, 104.0, 100.0),
            (104.0, 110.0, 101.0, 109.0, 100.0),
            (109.0, 114.0, 108.0, 113.0, 100.0),
        ],
        freq="1min",
    )

    unresolved = replay_intent(
        confluence(Side.LONG),
        candles=wide,
        candle_interval="5min",
        costs=costs(),
    )
    resolved = replay_intent(
        confluence(Side.LONG),
        candles=wide,
        candle_interval="5min",
        lower_native_bars=lower,
        lower_native_interval="1min",
        costs=costs(),
    )

    assert unresolved.trade is not None
    assert unresolved.trade.final_reason == "initial_stop"
    assert resolved.trade is not None
    assert [leg.reason for leg in resolved.trade.exit_legs] == [
        "partial_1r",
        "initial_target",
    ]


def test_partial_then_entry_stop_beats_target_when_both_overlap() -> None:
    candles = frame(
        [
            (100.0, 105.0, 99.0, 104.0, 100.0),
            (105.0, 114.0, 100.0, 108.0, 100.0),
        ]
    )

    result = replay_intent(
        confluence(Side.LONG),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.trade is not None
    assert [leg.reason for leg in result.trade.exit_legs] == [
        "partial_1r",
        "initial_stop",
    ]
    assert result.trade.exit_legs[-1].fill_price == result.trade.entry_price
    assert event_kinds(result) == ["entry_filled", "partial_1r", "breakeven_stop"]


def test_data_end_keeps_the_position_open_censored_without_forced_exit() -> None:
    candles = frame([(100.0, 105.0, 95.0, 102.0, 100.0)])

    result = replay_intent(
        confluence(Side.LONG),
        candles=candles,
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "OPEN_CENSORED"
    assert result.trade is None
    assert result.open_position is not None
    assert result.open_position.remaining_quantity == result.open_position.original_quantity
    assert event_kinds(result) == ["entry_filled", "open_censored"]


def test_volume_signal_closes_at_its_next_replay_open() -> None:
    rows: list[tuple[float, float, float, float, float]] = []
    for _ in range(20):
        rows.append((100.0, 101.0, 99.0, 100.0, 100.0))
    rows.append((100.0, 103.0, 100.0, 102.0, 200.0))
    rows.append((103.0, 104.0, 102.0, 103.0, 100.0))

    result = replay_intent(
        confluence(Side.LONG),
        candles=frame(rows),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.status == "CLOSED"
    assert result.trade is not None
    assert result.trade.final_reason == "volume_spike"
    assert result.trade.exit_legs[-1].fill_price == 103.0
    assert event_kinds(result)[-2:] == ["volume_exit_scheduled", "volume_spike"]


def test_resting_target_at_next_open_precedes_scheduled_volume_exit() -> None:
    rows: list[tuple[float, float, float, float, float]] = []
    for _ in range(20):
        rows.append((100.0, 101.0, 99.0, 100.0, 100.0))
    rows.append((100.0, 103.0, 100.0, 102.0, 200.0))
    rows.append((114.0, 115.0, 113.0, 114.0, 100.0))

    result = replay_intent(
        confluence(Side.LONG),
        candles=frame(rows),
        candle_interval="5min",
        costs=costs(),
    )

    assert result.trade is not None
    assert result.trade.final_reason == "initial_target"
    assert event_kinds(result)[-3:] == [
        "volume_exit_scheduled",
        "partial_1r",
        "initial_target",
    ]


def test_invalid_first_revisit_open_rejects_without_retry() -> None:
    result = replay_intent(
        confluence(Side.LONG),
        candles=frame(
            [
                (89.0, 101.0, 88.0, 100.0, 100.0),
                (100.0, 105.0, 99.0, 104.0, 100.0),
            ]
        ),
        candle_interval="5min",
        costs=costs(),
    )
    assert result.status == "ENTRY_REJECTED"
    assert result.rejection_reason == "first_revisit_open_outside_initial_stop"
    assert event_kinds(result) == ["entry_rejected"]
