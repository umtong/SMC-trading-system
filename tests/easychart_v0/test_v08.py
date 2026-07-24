from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ictbt.easychart_v0.domain import (
    FairValueGap,
    FormationBar,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.pipeline import FeatureBook, Opportunity, OpportunityRejection
from ictbt.easychart_v0.v08 import (
    TargetOwnershipReason,
    V08TargetPolicy,
    assemble_v08_opportunity,
    build_owned_terminal_targets,
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


def fixture_book(*, target_timeframe: Timeframe = Timeframe.M15) -> FeatureBook:
    a_bar = bar("2025-01-01 00:00", 15, 99.4, 100.0, 98.8, 99.5)
    b_bar = bar("2025-01-01 00:15", 15, 99.5, 102.0, 99.4, 101.5)
    c_bar = bar("2025-01-01 00:30", 15, 101.0, 102.0, 100.4, 101.2)
    execution_gap = FairValueGap(
        "execution-fvg",
        "BTCUSDT",
        Timeframe.M15,
        Side.LONG,
        (a_bar, b_bar, c_bar),
        PriceZone(100.0, 100.4),
        c_bar.close_time,
    )
    boundary = StrictPivot(
        "boundary",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        100.2,
        ts("2024-12-31 23:00"),
        ts("2024-12-31 23:30"),
    )
    target = StrictPivot(
        "target",
        "BTCUSDT",
        target_timeframe,
        "high",
        103.0,
        ts("2024-12-31 20:00"),
        ts("2024-12-31 23:55"),
    )
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = frame(
        (bar("2025-01-01 00:45", 5, 101.0, 101.5, 100.0, 101.3),)
    )
    frames[Timeframe.M15] = frame((a_bar, b_bar, c_bar))
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.M15] = (boundary,)
    pivots[target_timeframe] = (*pivots[target_timeframe], target)
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


def zero_costs() -> CostConfig:
    return CostConfig(0.0, 0.0, 0.0, 0.0)


def test_plain_m15_pivot_is_not_a_terminal_liquidity_owner() -> None:
    result = build_v08_scene_family_result(fixture_book(target_timeframe=Timeframe.M15))

    assert result.authorities == ()
    assert result.diagnostics.base_scenes == 1
    assert result.diagnostics.scenes_without_owned_terminal_target == 1


def test_h1_external_pivot_is_an_owned_terminal_target() -> None:
    result = build_v08_scene_family_result(fixture_book(target_timeframe=Timeframe.H1))

    assert len(result.authorities) == 1
    authority = result.authorities[0]
    assert authority.destination.source_id == "target"
    assert result.ownership[authority.authority_id].reason is (
        TargetOwnershipReason.HTF_EXTERNAL_PIVOT
    )


def test_unbacked_fvg_is_not_terminal_but_liquidity_backed_fvg_is() -> None:
    book = fixture_book(target_timeframe=Timeframe.H1)
    bars = (
        bar("2024-12-31 21:00", 60, 101.0, 102.5, 100.5, 102.0),
        bar("2024-12-31 22:00", 60, 102.0, 104.0, 101.8, 103.5),
        bar("2024-12-31 23:00", 60, 103.1, 104.2, 102.9, 103.8),
    )
    backed = FairValueGap(
        "backed-fvg",
        "BTCUSDT",
        Timeframe.H1,
        Side.SHORT,
        bars,
        PriceZone(102.9, 103.1),
        bars[-1].close_time,
    )
    unbacked = replace(
        backed,
        fvg_id="unbacked-fvg",
        zone=PriceZone(101.8, 102.0),
    )
    fvgs = dict(book.fvgs)
    fvgs[Timeframe.H1] = (unbacked, backed)
    book = replace(book, fvgs=fvgs)

    targets = build_owned_terminal_targets(
        book,
        side=Side.LONG,
        entry_price=100.2,
        as_of=ts("2025-01-01 00:45"),
    )
    by_source = {item.candidate.source_id: item for item in targets}

    assert "unbacked-fvg" not in by_source
    assert by_source["backed-fvg"].reason is (
        TargetOwnershipReason.FVG_AT_OWNED_LIQUIDITY
    )


def test_geometry_floor_does_not_force_every_trade_to_one_r() -> None:
    book = fixture_book(target_timeframe=Timeframe.H1)
    result = build_v08_scene_family_result(book)
    authority = result.authorities[0]
    # Stop distance is 1.5 and this target is 2.8 away: accepted at the 0.65R
    # floor.  Raise the floor above the available geometry to verify rejection.
    accepted = assemble_v08_opportunity(
        book,
        authority,
        costs=zero_costs(),
        entry_arm="first_return_limit",
        policy=V08TargetPolicy(minimum_target_r=0.65),
    )
    rejected = assemble_v08_opportunity(
        book,
        authority,
        costs=zero_costs(),
        entry_arm="first_return_limit",
        policy=V08TargetPolicy(minimum_target_r=2.0),
    )

    assert isinstance(accepted, Opportunity)
    assert isinstance(rejected, OpportunityRejection)
    assert rejected.reason == "target_space_conflict"
