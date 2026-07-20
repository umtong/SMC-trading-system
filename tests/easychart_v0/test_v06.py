from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import (
    FormationBar,
    ObKind,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.pipeline import FeatureBook
from ictbt.easychart_v0.v06 import (
    _anchor_is_fresh_before_partner_formation,
    _owned_m15_break_pivot,
    build_owned_m15_overlap_result,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(
    opened: str,
    minutes: int,
    o: float,
    h: float,
    l: float,
    c: float,
) -> FormationBar:
    start = ts(opened)
    return FormationBar(
        start,
        start + pd.Timedelta(minutes=minutes),
        o,
        h,
        l,
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


def block(
    block_id: str,
    timeframe: Timeframe,
    side: Side,
    bars: tuple[FormationBar, FormationBar],
    zone: PriceZone,
) -> OrderBlock:
    extreme = (
        min(item.low for item in bars)
        if side is Side.LONG
        else max(item.high for item in bars)
    )
    return OrderBlock(
        block_id,
        "BTCUSDT",
        timeframe,
        ObKind.SIMPLE_2C,
        side,
        bars,
        zone,
        bars[-1].close_time,
        extreme,
        extreme - 0.1 if side is Side.LONG else extreme + 0.1,
        max(item.high for item in bars)
        if side is Side.LONG
        else min(item.low for item in bars),
    )


def fixture_book(*, m5_first_low: float = 101.0) -> tuple[FeatureBook, OrderBlock]:
    m15_bars = (
        bar("2024-12-31 23:00", 15, 103, 104, 102, 103),
        bar("2024-12-31 23:15", 15, 103, 104, 102, 103),
        bar("2024-12-31 23:30", 15, 103, 104, 102, 103),
        bar("2024-12-31 23:45", 15, 103, 104, 102, 103),
        bar("2025-01-01 00:00", 15, 99, 100, 97.8, 98.5),
        bar("2025-01-01 00:15", 15, 100, 101, 98, 100.5),
        bar("2025-01-01 00:30", 15, 100.5, 101, 99, 100),
        bar("2025-01-01 00:45", 15, 100, 100.5, 98.8, 99),
        bar("2025-01-01 01:00", 15, 99, 102.2, 98.5, 102),
    )
    anchor = block(
        "anchor",
        Timeframe.M15,
        Side.LONG,
        (m15_bars[-2], m15_bars[-1]),
        PriceZone(99, 100),
    )
    m5_bars = (
        bar("2025-01-01 00:55", 5, 99.8, 100.2, 99.4, 99.6),
        bar("2025-01-01 01:00", 5, 99.6, 100.3, 99.2, 100.1),
        bar("2025-01-01 01:05", 5, 100.1, 101.2, 99.7, 101),
        bar("2025-01-01 01:10", 5, 101, 102.2, 101, 102),
        bar("2025-01-01 01:15", 5, 102, 102.2, m5_first_low, 101.5),
    )
    partner = block(
        "partner",
        Timeframe.M5,
        Side.LONG,
        (m5_bars[0], m5_bars[1]),
        PriceZone(99.6, 99.8),
    )
    pivots = (
        StrictPivot(
            "external-high",
            "BTCUSDT",
            Timeframe.M15,
            "high",
            105,
            ts("2024-12-31 22:00"),
            ts("2024-12-31 22:45"),
        ),
        StrictPivot(
            "protected-low",
            "BTCUSDT",
            Timeframe.M15,
            "low",
            98,
            ts("2025-01-01 00:00"),
            ts("2025-01-01 00:45"),
        ),
        StrictPivot(
            "break-high",
            "BTCUSDT",
            Timeframe.M15,
            "high",
            101,
            ts("2025-01-01 00:15"),
            ts("2025-01-01 00:45"),
        ),
    )
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = frame(m5_bars)
    frames[Timeframe.M15] = frame(m15_bars)
    order_blocks = {timeframe: () for timeframe in Timeframe}
    order_blocks[Timeframe.M5] = (partner,)
    order_blocks[Timeframe.M15] = (anchor,)
    pivot_map = {timeframe: () for timeframe in Timeframe}
    pivot_map[Timeframe.M15] = pivots
    book = FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks=order_blocks,
        pivots=pivot_map,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )
    return book, anchor


def test_anchor_final_bar_owns_latest_known_m15_break() -> None:
    book, anchor = fixture_book()
    pivot = _owned_m15_break_pivot(book, anchor)
    assert pivot is not None
    assert pivot.pivot_id == "break-high"


def test_owned_break_does_not_fall_back_to_an_older_easier_pivot() -> None:
    book, anchor = fixture_book()
    harder = StrictPivot(
        "harder-latest-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        103,
        ts("2025-01-01 00:30"),
        ts("2025-01-01 00:55"),
    )
    book.pivots[Timeframe.M15] = (*book.pivots[Timeframe.M15], harder)
    assert _owned_m15_break_pivot(book, anchor) is None


def test_partner_formation_touch_is_construction_but_earlier_touch_is_not() -> None:
    book, anchor = fixture_book()
    later_bars = (
        bar("2025-01-01 01:20", 5, 100.5, 100.8, 99.5, 99.7),
        bar("2025-01-01 01:25", 5, 99.7, 101.5, 99.4, 101),
    )
    later = block(
        "later-partner",
        Timeframe.M5,
        Side.LONG,
        later_bars,
        PriceZone(99.7, 100.5),
    )
    assert _anchor_is_fresh_before_partner_formation(book, anchor, later)

    touched_book, touched_anchor = fixture_book(m5_first_low=99.5)
    assert not _anchor_is_fresh_before_partner_formation(
        touched_book,
        touched_anchor,
        later,
    )


def test_stop_arms_share_scene_zone_and_target_and_only_change_stop() -> None:
    book, _ = fixture_book()
    formation = build_owned_m15_overlap_result(
        book,
        stop_owner="m15_anchor_formation",
    )
    protected = build_owned_m15_overlap_result(
        book,
        stop_owner="protected_m15_swing",
    )
    assert len(formation.authorities) == len(protected.authorities) == 1
    left = formation.authorities[0]
    right = protected.authorities[0]
    assert left.authority_id == right.authority_id
    assert left.zone == right.zone
    assert left.destination == right.destination
    assert left.anchor_ob == right.anchor_ob
    assert left.partner_ob == right.partner_ob
    assert left.initial_stop != right.initial_stop
    assert left.stop_owner == "m15_anchor_formation"
    assert right.stop_owner == "protected_m15_swing"
    assert left.pair_type == "m15_m5"
    assert left.entry_mode.value == "limit_first_revisit"
