from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import (
    FairValueGap,
    FormationBar,
    LiquidityDeliveryAuthority,
    LiquidityEvent,
    ObKind,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.pipeline import FeatureBook, Opportunity
from ictbt.easychart_v0.v04 import assemble_v04_opportunity
from ictbt.easychart_v0.v05 import (
    _external_destination_at_event,
    _fvg_candidate,
    _ob_candidate,
    _refine_entry_zone,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(opened: str, minutes: int, o: float, h: float, l: float, c: float) -> FormationBar:
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


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def frame_from_bars(bars: tuple[FormationBar, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in bars
        ],
        index=pd.DatetimeIndex(
            [item.open_time for item in bars],
            name="open_time",
        ),
        dtype=float,
    )


def order_block(
    block_id: str,
    timeframe: Timeframe,
    side: Side,
    bars: tuple[FormationBar, FormationBar],
    zone: PriceZone,
    stop_extreme: float,
    initial_stop: float,
) -> OrderBlock:
    return OrderBlock(
        block_id,
        "BTCUSDT",
        timeframe,
        ObKind.SIMPLE_2C,
        side,
        bars,
        zone,
        bars[-1].close_time,
        stop_extreme,
        initial_stop,
        max(item.high for item in bars)
        if side is Side.LONG
        else min(item.low for item in bars),
    )


def location(zone: PriceZone = PriceZone(98.5, 99.5)) -> OrderBlock:
    return order_block(
        "m15-location",
        Timeframe.M15,
        Side.LONG,
        (
            bar("2025-01-01 00:00", 15, 100, 101, 98, 99),
            bar("2025-01-01 00:15", 15, 99, 102, 98.5, 101),
        ),
        zone,
        98,
        97.9,
    )


def sweep_event() -> LiquidityEvent:
    return LiquidityEvent(
        "m5-sweep",
        "BTCUSDT",
        Timeframe.M5,
        "sweep_reclaim",
        Side.LONG,
        "m15-low",
        99,
        ts("2025-01-01 00:30"),
        ts("2025-01-01 00:35"),
        98,
    )


def m5_break_pivot() -> StrictPivot:
    return StrictPivot(
        "m5-high",
        "BTCUSDT",
        Timeframe.M5,
        "high",
        101,
        ts("2025-01-01 00:00"),
        ts("2025-01-01 00:20"),
    )


def book(
    *,
    m5: pd.DataFrame | None = None,
    m15: pd.DataFrame | None = None,
    order_blocks: dict[Timeframe, tuple[OrderBlock, ...]] | None = None,
    pivots: dict[Timeframe, tuple[StrictPivot, ...]] | None = None,
    fvgs: dict[Timeframe, tuple[FairValueGap, ...]] | None = None,
) -> FeatureBook:
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    if m5 is not None:
        frames[Timeframe.M5] = m5
    if m15 is not None:
        frames[Timeframe.M15] = m15
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks=(
            {timeframe: () for timeframe in Timeframe}
            if order_blocks is None
            else order_blocks
        ),
        pivots=(
            {timeframe: () for timeframe in Timeframe}
            if pivots is None
            else pivots
        ),
        fvgs=(
            {timeframe: () for timeframe in Timeframe}
            if fvgs is None
            else fvgs
        ),
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def fvg(*, displacement_close: float = 101.5) -> FairValueGap:
    bars = (
        bar("2025-01-01 00:30", 5, 99.5, 100, 98, 99.8),
        bar(
            "2025-01-01 00:35",
            5,
            99.8,
            max(102, displacement_close),
            99.8,
            displacement_close,
        ),
        bar("2025-01-01 00:40", 5, 100.5, 102, 100.2, 101.8),
    )
    return FairValueGap(
        "m5-fvg",
        "BTCUSDT",
        Timeframe.M5,
        Side.LONG,
        bars,
        PriceZone(100, 100.2),
        bars[-1].close_time,
    )


def test_fvg_central_displacement_bar_must_own_the_m5_break() -> None:
    gap = fvg()
    item = _fvg_candidate(
        book(
            m5=frame_from_bars(gap.formation_bars),
            pivots={
                **{timeframe: () for timeframe in Timeframe},
                Timeframe.M5: (m5_break_pivot(),),
            },
        ),
        event=sweep_event(),
        location=location(),
        gap=gap,
    )
    assert item is not None
    assert item.pivot.pivot_id == "m5-high"
    assert item.stop_owner == "m5_fvg_formation"
    assert item.initial_stop == 97.9


def test_fvg_is_rejected_when_only_its_c_bar_breaks_structure() -> None:
    gap = fvg(displacement_close=100.5)
    assert (
        _fvg_candidate(
            book(
                m5=frame_from_bars(gap.formation_bars),
                pivots={
                    **{timeframe: () for timeframe in Timeframe},
                    Timeframe.M5: (m5_break_pivot(),),
                },
            ),
            event=sweep_event(),
            location=location(),
            gap=gap,
        )
        is None
    )


def test_fvg_delivery_formula_is_symmetric_for_short() -> None:
    bars = (
        bar("2025-01-01 00:30", 5, 100.5, 102, 100, 100.2),
        bar("2025-01-01 00:35", 5, 100.2, 100.2, 98, 98.5),
        bar("2025-01-01 00:40", 5, 98.8, 99.8, 98.1, 98.5),
    )
    gap = FairValueGap(
        "short-fvg",
        "BTCUSDT",
        Timeframe.M5,
        Side.SHORT,
        bars,
        PriceZone(99.8, 100),
        bars[-1].close_time,
    )
    event = LiquidityEvent(
        "short-sweep",
        "BTCUSDT",
        Timeframe.M5,
        "sweep_reclaim",
        Side.SHORT,
        "m15-high",
        101,
        ts("2025-01-01 00:30"),
        ts("2025-01-01 00:35"),
        102,
    )
    pivot = StrictPivot(
        "m5-low",
        "BTCUSDT",
        Timeframe.M5,
        "low",
        99,
        ts("2025-01-01 00:00"),
        ts("2025-01-01 00:20"),
    )
    short_location = order_block(
        "short-location",
        Timeframe.M15,
        Side.SHORT,
        (
            bar("2025-01-01 00:00", 15, 99, 101, 98.5, 100),
            bar("2025-01-01 00:15", 15, 100, 101.5, 98, 99),
        ),
        PriceZone(100.5, 101.5),
        101.5,
        101.6,
    )
    item = _fvg_candidate(
        book(
            m5=frame_from_bars(bars),
            pivots={
                **{timeframe: () for timeframe in Timeframe},
                Timeframe.M5: (pivot,),
            },
        ),
        event=event,
        location=short_location,
        gap=gap,
    )
    assert item is not None
    assert item.pivot.pivot_id == "m5-low"
    assert item.stop_owner == "m5_fvg_formation"
    assert item.initial_stop == 102.1


def test_ob_uses_event_stop_when_formation_does_not_cover_sweep_extreme() -> None:
    bars = (
        bar("2025-01-01 00:35", 5, 100.6, 100.8, 100, 100.1),
        bar("2025-01-01 00:40", 5, 100, 102, 99.9, 101.5),
    )
    block = order_block(
        "m5-ob",
        Timeframe.M5,
        Side.LONG,
        bars,
        PriceZone(100.1, 100.6),
        99.9,
        99.8,
    )
    item = _ob_candidate(
        book(
            m5=frame_from_bars(bars),
            pivots={
                **{timeframe: () for timeframe in Timeframe},
                Timeframe.M5: (m5_break_pivot(),),
            },
        ),
        event=sweep_event(),
        location=location(PriceZone(100.2, 100.4)),
        block=block,
    )
    assert item is not None
    assert item.stop_owner == "m15_event"
    assert item.stop_extreme == 98
    assert item.initial_stop == 97.9
    assert item.zone == PriceZone(100.2, 100.4)
    assert item.entry_zone_source == "m15_m5_intersection"


def test_entry_zone_uses_execution_zone_when_m15_has_no_overlap() -> None:
    zone, source = _refine_entry_zone(
        location(),
        PriceZone(100, 100.2),
        base_source="fvg_wick_gap",
        tick_size=0.1,
    )
    assert zone == PriceZone(100, 100.2)
    assert source == "fvg_wick_gap"


def test_event_destination_prefers_latest_m15_range_boundary_over_near_ob() -> None:
    event = LiquidityEvent(
        "event",
        "BTCUSDT",
        Timeframe.M5,
        "sweep_reclaim",
        Side.LONG,
        "node",
        99,
        ts("2025-01-01 00:45"),
        ts("2025-01-01 01:00"),
        98,
    )
    older = StrictPivot(
        "older-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        110,
        ts("2025-01-01 00:00"),
        ts("2025-01-01 00:40"),
    )
    latest = StrictPivot(
        "latest-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        105,
        ts("2025-01-01 00:15"),
        ts("2025-01-01 00:50"),
    )
    opposing = order_block(
        "near-short-ob",
        Timeframe.M15,
        Side.SHORT,
        (
            bar("2025-01-01 00:15", 15, 101, 102, 100, 101.5),
            bar("2025-01-01 00:30", 15, 101.5, 102, 99.5, 100.5),
        ),
        PriceZone(100, 101),
        102,
        102.1,
    )
    m15 = frame_from_bars(
        (
            *opposing.formation_bars,
            bar("2025-01-01 00:45", 15, 100.5, 104, 100, 100.5),
        )
    )
    target = _external_destination_at_event(
        book(
            m15=m15,
            order_blocks={
                **{timeframe: () for timeframe in Timeframe},
                Timeframe.M15: (opposing,),
            },
            pivots={
                **{timeframe: () for timeframe in Timeframe},
                Timeframe.M15: (older, latest),
            },
        ),
        event,
    )
    assert target is not None
    assert target.source_id == "latest-high"
    assert target.order_price == 105


def test_v05_authority_uses_common_first_revisit_opportunity_contract() -> None:
    gap = fvg()
    feature_book = book(
        m5=frame_from_bars(gap.formation_bars),
        pivots={
            **{timeframe: () for timeframe in Timeframe},
            Timeframe.M5: (m5_break_pivot(),),
        },
    )
    candidate = _fvg_candidate(
        feature_book,
        event=sweep_event(),
        location=location(),
        gap=gap,
    )
    assert candidate is not None
    target = TargetCandidate(
        "target",
        "BTCUSDT",
        Side.LONG,
        "pivot",
        PriceZone(105, 105),
        sweep_event().known_at,
        "m15-high",
    )
    authority = LiquidityDeliveryAuthority(
        "v05-scene",
        "BTCUSDT",
        Side.LONG,
        location(),
        sweep_event(),
        candidate.kind,
        candidate.delivery_root_id,
        candidate.pivot,
        candidate.order_block,
        candidate.fvg,
        candidate.zone,
        candidate.entry_zone_source,
        candidate.known_at,
        candidate.stop_owner,
        candidate.stop_extreme,
        candidate.initial_stop,
        candidate.impulse_extreme,
        target,
    )
    result = assemble_v04_opportunity(
        feature_book,
        authority,
        costs=CostConfig(0, 0, 0, 0, 0, 0),
    )
    assert isinstance(result, Opportunity)
    assert result.planned_entry.price == candidate.zone.high
    assert result.initial_stop == 97.9
    assert result.target.order_price == 105
