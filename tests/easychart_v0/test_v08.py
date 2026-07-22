from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import (
    FairValueGap,
    FormationBar,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.pipeline import FeatureBook
from ictbt.easychart_v0.v08 import (
    V08ContextPolicy,
    V08TargetPolicy,
    build_v08_scene_family_result,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(
    opened: str,
    minutes: int,
    o: float,
    h: float,
    low: float,
    c: float,
) -> FormationBar:
    start = ts(opened)
    return FormationBar(
        start,
        start + pd.Timedelta(minutes=minutes),
        o,
        h,
        low,
        c,
        100.0,
    )


def frame(items: tuple[FormationBar, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in items
        ],
        index=pd.DatetimeIndex(
            [item.open_time for item in items],
            name="open_time",
        ),
        dtype=float,
    )


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def fixture_book() -> FeatureBook:
    a_bar = bar("2025-01-01 00:00", 15, 99.4, 100.0, 98.8, 99.5)
    b_bar = bar("2025-01-01 00:15", 15, 99.5, 102.0, 99.4, 101.5)
    c_bar = bar("2025-01-01 00:30", 15, 101.0, 102.0, 100.4, 101.2)
    execution_gap = FairValueGap(
        "execution-fvg",
        "BTCUSDT",
        Timeframe.M15,
        Side.LONG,
        (a_bar, b_bar, c_bar),
        PriceZone(a_bar.high, c_bar.low),
        c_bar.close_time,
    )
    boundary = StrictPivot(
        "valid-m15-boundary",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        100.2,
        ts("2024-12-31 23:00"),
        ts("2024-12-31 23:30"),
    )
    same_h1 = StrictPivot(
        "same-h1-boundary",
        "BTCUSDT",
        Timeframe.H1,
        "high",
        100.2,
        ts("2024-12-31 21:00"),
        ts("2024-12-31 23:00"),
    )
    target = StrictPivot(
        "known-target-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        103.0,
        ts("2024-12-31 23:30"),
        ts("2024-12-31 23:55"),
    )
    m5_bars = (
        bar("2025-01-01 00:45", 5, 101.3, 101.6, 101.0, 101.4),
        bar("2025-01-01 00:50", 5, 101.4, 101.7, 101.1, 101.5),
    )
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = frame(m5_bars)
    frames[Timeframe.M15] = frame((a_bar, b_bar, c_bar))
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.M15] = (boundary, target)
    pivots[Timeframe.H1] = (same_h1,)
    fvgs = {timeframe: () for timeframe in Timeframe}
    fvgs[Timeframe.M15] = (execution_gap,)
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots=pivots,
        fvgs=fvgs,
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def test_pivot_only_uses_only_target_known_by_scene_completion() -> None:
    book = fixture_book()
    future = StrictPivot(
        "future-closer-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        102.0,
        ts("2025-01-01 00:40"),
        ts("2025-01-01 01:10"),
    )
    pivots = dict(book.pivots)
    pivots[Timeframe.M15] = (*pivots[Timeframe.M15], future)
    book = replace(book, pivots=pivots)

    result = build_v08_scene_family_result(
        book,
        target_policy=V08TargetPolicy.PIVOT_ONLY,
        context_policy=V08ContextPolicy.NONE,
    )

    assert len(result.authorities) == 1
    assert result.authorities[0].destination.source_id == "known-target-high"
    assert result.authorities[0].destination.known_at <= result.authorities[0].known_at


def test_not_opposed_allows_range_but_strict_delivery_rejects_it() -> None:
    book = fixture_book()
    not_opposed = build_v08_scene_family_result(
        book,
        target_policy="pivot_only",
        context_policy="not_opposed",
    )
    strict = build_v08_scene_family_result(
        book,
        target_policy="pivot_only",
        context_policy="strict_delivery",
    )

    assert len(not_opposed.authorities) == 1
    assert strict.authorities == ()
    assert strict.diagnostics.context_rejections == 1


def test_zone_confluent_pivot_requires_preknown_active_opposing_zone() -> None:
    book = fixture_book()
    without_zone = build_v08_scene_family_result(
        book,
        target_policy="pivot_zone_confluent",
        context_policy="none",
    )
    assert without_zone.authorities == ()
    assert without_zone.diagnostics.target_missing == 1

    a = bar("2024-12-31 18:00", 60, 104.5, 105.0, 103.8, 104.4)
    b = bar("2024-12-31 19:00", 60, 104.4, 104.8, 102.7, 103.1)
    c = bar("2024-12-31 20:00", 60, 103.1, 103.2, 102.4, 102.8)
    opposing = FairValueGap(
        "opposing-h1-fvg",
        "BTCUSDT",
        Timeframe.H1,
        Side.SHORT,
        (a, b, c),
        PriceZone(102.8, 103.2),
        c.close_time,
    )
    fvgs = dict(book.fvgs)
    fvgs[Timeframe.H1] = (opposing,)
    with_zone = build_v08_scene_family_result(
        replace(book, fvgs=fvgs),
        target_policy="pivot_zone_confluent",
        context_policy="none",
    )

    assert len(with_zone.authorities) == 1
    assert with_zone.authorities[0].destination.source_id == "known-target-high"
    assert with_zone.authorities[0].destination.kind == "pivot"


def test_baseline_policy_preserves_frozen_v07_destination() -> None:
    result = build_v08_scene_family_result(
        fixture_book(),
        target_policy="baseline_nearest_any",
        context_policy="none",
    )

    assert len(result.authorities) == 1
    assert result.authorities[0].destination.source_id == "known-target-high"
    assert result.diagnostics.destination_changed == 0


def test_unknown_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_v08_scene_family_result(
            fixture_book(),
            target_policy="future_optimized_policy",
            context_policy="none",
        )
