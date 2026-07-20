from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0 import (
    ObKind,
    PriceZone,
    Side,
    Timeframe,
    detect_double_order_blocks,
    detect_fvgs,
    detect_simple_order_blocks,
    detect_strict_pivots,
    impulse_is_consumed,
    intersect_zones,
    merge_zones,
    pivot_is_consumed,
    resample_completed,
    zone_is_consumed,
)


def frame(
    rows: list[tuple[float, float, float, float, float]],
    *,
    start: str = "2026-01-01T00:00:00Z",
    frequency: str = "5min",
) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        index=pd.date_range(start, periods=len(rows), freq=frequency),
        columns=["open", "high", "low", "close", "volume"],
    )


def test_resample_keeps_only_complete_utc_bars() -> None:
    candles = frame(
        [
            (100.0 + index, 101.0 + index, 99.0 + index, 100.5 + index, 10.0)
            for index in range(49)
        ]
    )

    m15 = resample_completed(candles, Timeframe.M15)
    h1 = resample_completed(candles, Timeframe.H1)
    h4 = resample_completed(candles, Timeframe.H4)

    assert len(m15) == 16
    assert len(h1) == 4
    assert len(h4) == 1
    assert h4.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 148.0,
        "low": 99.0,
        "close": 147.5,
        "volume": 480.0,
    }


def test_simple_2c_uses_origin_body_and_full_formation_extremes() -> None:
    candles = frame(
        [
            (101.0, 102.0, 99.0, 100.0, 10.0),
            (99.5, 103.0, 98.8, 102.0, 12.0),
        ]
    )

    block = detect_simple_order_blocks(
        candles, symbol="BTCUSDT", timeframe=Timeframe.M5, tick_size=0.1
    )[0]

    assert block.kind is ObKind.SIMPLE_2C
    assert block.side is Side.LONG
    assert block.zone == PriceZone(100.0, 101.0)
    assert block.stop_extreme == 98.8
    assert block.initial_stop == pytest.approx(98.7)
    assert block.impulse_extreme == 103.0
    assert block.known_at == pd.Timestamp("2026-01-01T00:10:00Z")


def test_double_3c_uses_middle_body_and_all_three_extremes() -> None:
    candles = frame(
        [
            (100.0, 101.2, 99.8, 101.0, 10.0),
            (101.5, 102.0, 99.2, 99.5, 11.0),
            (99.0, 103.0, 98.8, 102.0, 12.0),
        ]
    )

    block = detect_double_order_blocks(
        candles, symbol="BTCUSDT", timeframe=Timeframe.M5, tick_size=0.1
    )[0]

    assert block.kind is ObKind.DOUBLE_3C
    assert block.side is Side.LONG
    assert block.zone == PriceZone(99.5, 101.5)
    assert block.stop_extreme == 98.8
    assert block.initial_stop == pytest.approx(98.7)
    assert block.impulse_extreme == 103.0


def test_strict_five_bar_pivot_rejects_equal_high() -> None:
    unique = frame(
        [
            (8.0, 10.0, 7.0, 9.0, 1.0),
            (9.0, 11.0, 8.0, 10.0, 1.0),
            (10.0, 15.0, 9.0, 11.0, 1.0),
            (10.0, 12.0, 8.0, 11.0, 1.0),
            (9.0, 11.0, 7.0, 10.0, 1.0),
        ]
    )
    tied = frame(
        [
            (8.0, 10.0, 7.0, 9.0, 1.0),
            (9.0, 11.0, 8.0, 10.0, 1.0),
            (10.0, 15.0, 9.0, 11.0, 1.0),
            (10.0, 15.0, 8.0, 11.0, 1.0),
            (9.0, 11.0, 7.0, 10.0, 1.0),
        ]
    )

    pivots = detect_strict_pivots(
        unique, symbol="BTCUSDT", timeframe=Timeframe.M5
    )
    tied_pivots = detect_strict_pivots(
        tied, symbol="BTCUSDT", timeframe=Timeframe.M5
    )

    assert [(pivot.kind, pivot.price) for pivot in pivots] == [("high", 15.0)]
    assert not tied_pivots
    assert pivots[0].known_at == pd.Timestamp("2026-01-01T00:25:00Z")


def test_fvg_requires_at_least_one_tick_of_wick_gap() -> None:
    exact_tick = frame(
        [
            (100.0, 101.0, 99.0, 100.5, 1.0),
            (100.5, 102.0, 100.0, 101.5, 1.0),
            (101.2, 102.5, 101.1, 102.0, 1.0),
        ]
    )
    sub_tick = exact_tick.copy()
    sub_tick.iloc[2, sub_tick.columns.get_loc("low")] = 101.05

    gaps = detect_fvgs(
        exact_tick, symbol="BTCUSDT", timeframe=Timeframe.M5, tick_size=0.1
    )
    too_small = detect_fvgs(
        sub_tick, symbol="BTCUSDT", timeframe=Timeframe.M5, tick_size=0.1
    )

    assert len(gaps) == 1
    assert gaps[0].side is Side.LONG
    assert gaps[0].zone == PriceZone(101.0, 101.1)
    assert not too_small


def test_zone_intersection_and_merge_are_inclusive() -> None:
    intersection = intersect_zones(
        [PriceZone(100.0, 102.0), PriceZone(101.0, 103.0)],
        minimum_width=1.0,
    )
    too_narrow = intersect_zones(
        [PriceZone(100.0, 102.0), PriceZone(101.0, 103.0)],
        minimum_width=1.1,
    )
    merged = merge_zones(
        [PriceZone(103.0, 104.0), PriceZone(100.0, 101.0), PriceZone(101.0, 102.0)]
    )

    assert intersection == PriceZone(101.0, 102.0)
    assert too_narrow is None
    assert merged == (PriceZone(100.0, 102.0), PriceZone(103.0, 104.0))


def test_pivot_impulse_and_zone_consumption_use_strict_tick_boundaries() -> None:
    pivot_source = frame(
        [
            (8.0, 10.0, 7.0, 9.0, 1.0),
            (9.0, 11.0, 8.0, 10.0, 1.0),
            (10.0, 15.0, 9.0, 11.0, 1.0),
            (10.0, 12.0, 8.0, 11.0, 1.0),
            (9.0, 11.0, 7.0, 10.0, 1.0),
        ]
    )
    pivot = detect_strict_pivots(
        pivot_source, symbol="BTCUSDT", timeframe=Timeframe.M5
    )[0]
    pivot_later = pd.concat(
        [
            pivot_source,
            frame(
                [(14.0, 15.1, 13.0, 14.5, 1.0)],
                start="2026-01-01T00:25:00Z",
            ),
        ]
    )

    ob_source = frame(
        [
            (101.0, 102.0, 99.0, 100.0, 10.0),
            (99.5, 103.0, 98.8, 102.0, 12.0),
        ]
    )
    block = detect_simple_order_blocks(
        ob_source, symbol="BTCUSDT", timeframe=Timeframe.M5, tick_size=0.1
    )[0]
    ob_later = pd.concat(
        [
            ob_source,
            frame(
                [(102.0, 103.1, 101.0, 102.5, 1.0)],
                start="2026-01-01T00:10:00Z",
            ),
        ]
    )

    assert pivot_is_consumed(pivot, pivot_later, tick_size=0.1)
    assert impulse_is_consumed(block, ob_later, tick_size=0.1)
    assert zone_is_consumed(
        PriceZone(105.0, 106.0),
        frame([(105.5, 106.2, 105.0, 106.1, 1.0)]),
        travel_side=Side.LONG,
        timeframe=Timeframe.M5,
        tick_size=0.1,
    )
