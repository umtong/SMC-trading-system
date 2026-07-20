from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import Side, StrictPivot, Timeframe
from ictbt.easychart_v0.pipeline import FeatureBook
from ictbt.easychart_v0.target_ownership import (
    PivotOwnershipReason,
    owned_pivot_targets,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def pivot(
    pivot_id: str,
    *,
    timeframe: Timeframe,
    price: float,
    known_at: str,
) -> StrictPivot:
    known = ts(known_at)
    return StrictPivot(
        pivot_id=pivot_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind="high",
        price=price,
        pivot_time=known - pd.Timedelta(hours=1),
        known_at=known,
    )


def book_with_pivots(items: tuple[StrictPivot, ...]) -> FeatureBook:
    pivots = {timeframe: () for timeframe in Timeframe}
    for item in items:
        pivots[item.timeframe] = (*pivots[item.timeframe], item)
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames={timeframe: empty_frame() for timeframe in Timeframe},
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots=pivots,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def test_lone_m15_pivot_cannot_own_a_terminal_target() -> None:
    book = book_with_pivots(
        (
            pivot(
                "lone-m15",
                timeframe=Timeframe.M15,
                price=105.0,
                known_at="2025-01-01 06:00",
            ),
        )
    )

    targets = owned_pivot_targets(
        book,
        side=Side.LONG,
        entry_reference=100.0,
        as_of=ts("2025-01-01 12:00"),
        preexisting_before=ts("2025-01-01 10:00"),
    )

    assert targets == ()


def test_equal_m15_pool_and_h1_external_pivot_own_targets() -> None:
    book = book_with_pivots(
        (
            pivot(
                "equal-a",
                timeframe=Timeframe.M15,
                price=105.0,
                known_at="2025-01-01 05:00",
            ),
            pivot(
                "equal-b",
                timeframe=Timeframe.M15,
                price=105.1,
                known_at="2025-01-01 06:00",
            ),
            pivot(
                "external-h1",
                timeframe=Timeframe.H1,
                price=110.0,
                known_at="2025-01-01 07:00",
            ),
        )
    )

    targets = owned_pivot_targets(
        book,
        side=Side.LONG,
        entry_reference=100.0,
        as_of=ts("2025-01-01 12:00"),
        preexisting_before=ts("2025-01-01 10:00"),
    )
    by_source = {target.candidate.source_id: target for target in targets}

    assert set(by_source) == {"equal-a", "equal-b", "external-h1"}
    assert by_source["equal-a"].reason is (
        PivotOwnershipReason.M15_EQUAL_LEVEL_POOL
    )
    assert set(by_source["equal-a"].evidence_ids) == {"equal-a", "equal-b"}
    assert by_source["external-h1"].reason is PivotOwnershipReason.HTF_EXTERNAL


def test_pivot_known_after_liquidity_event_cannot_retroactively_own_target() -> None:
    book = book_with_pivots(
        (
            pivot(
                "late-h1",
                timeframe=Timeframe.H1,
                price=110.0,
                known_at="2025-01-01 11:00",
            ),
        )
    )

    targets = owned_pivot_targets(
        book,
        side=Side.LONG,
        entry_reference=100.0,
        as_of=ts("2025-01-01 12:00"),
        preexisting_before=ts("2025-01-01 10:00"),
    )

    assert targets == ()
