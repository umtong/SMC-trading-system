from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import (
    EntryMode,
    FairValueGap,
    FormationBar,
    ObKind,
    OrderBlock,
    PriceZone,
    SceneFamily,
    Side,
    StrictPivot,
    StructureFlipAuthority,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import FeatureBook, Opportunity
from ictbt.easychart_v0.v07 import (
    V07ExecutionArm,
    assemble_v07_opportunity,
    build_v07_scene_family_result,
    run_v07_historical_replay,
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


def fixture_book(
    *,
    boundary_price: float = 100.2,
    c_low: float = 100.4,
    c_high: float = 102.0,
    c_close: float = 101.2,
    next_open: float = 101.3,
    target_price: float = 103.0,
) -> FeatureBook:
    a_bar = bar("2025-01-01 00:00", 15, 99.4, 100.0, 98.8, 99.5)
    b_bar = bar("2025-01-01 00:15", 15, 99.5, 102.0, 99.4, 101.5)
    c_bar = bar("2025-01-01 00:30", 15, 101.0, c_high, c_low, c_close)
    execution_gap = FairValueGap(
        "execution-fvg",
        "BTCUSDT",
        Timeframe.M15,
        Side.LONG,
        (a_bar, b_bar, c_bar),
        PriceZone(a_bar.high, c_bar.low),
        c_bar.close_time,
    )

    valid_m15 = StrictPivot(
        "valid-m15-boundary",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        boundary_price,
        ts("2024-12-31 23:00"),
        ts("2024-12-31 23:30"),
    )
    same_h1 = StrictPivot(
        "same-h1-boundary",
        "BTCUSDT",
        Timeframe.H1,
        "high",
        boundary_price,
        ts("2024-12-31 21:00"),
        ts("2024-12-31 23:00"),
    )
    # This is newer than the valid M15 boundary, but B never breaks it.  It is
    # also the frozen target once the complete scene has selected its boundary.
    unbroken_target = StrictPivot(
        "newer-unbroken-high",
        "BTCUSDT",
        Timeframe.M15,
        "high",
        target_price,
        ts("2024-12-31 23:30"),
        ts("2024-12-31 23:55"),
    )

    m5_bars = (
        bar(
            "2025-01-01 00:45",
            5,
            next_open,
            max(next_open, 101.6),
            min(next_open, 101.0),
            min(next_open, 101.4),
        ),
        bar("2025-01-01 00:50", 5, 101.4, 101.7, 101.1, 101.5),
    )
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = frame(m5_bars)
    frames[Timeframe.M15] = frame((a_bar, b_bar, c_bar))
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.M15] = (valid_m15, unbroken_target)
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


def zero_costs() -> CostConfig:
    return CostConfig(0.0, 0.0, 0.0, 0.0)


def test_builder_filters_complete_matches_before_preferring_m15_boundary() -> None:
    result = build_v07_scene_family_result(fixture_book())

    assert len(result.authorities) == 1
    authority = result.authorities[0]
    assert authority.scene_family is SceneFamily.SR_FLIP_FVG
    assert authority.boundary_pivot.pivot_id == "valid-m15-boundary"
    assert authority.boundary_pivot.timeframe is Timeframe.M15
    assert authority.zone == PriceZone(100.2, 100.2)
    assert authority.destination.source_id == "newer-unbroken-high"
    assert authority.destination.order_price == 103.0
    assert authority.known_at == ts("2025-01-01 00:45")
    assert result.diagnostics.directional_breaks == 1
    assert result.diagnostics.boundary_linked_fvgs == 1


@pytest.mark.parametrize(
    ("kwargs", "failed_stage"),
    [
        ({"c_low": 100.2, "c_close": 100.25}, "accepted_breaks"),
        ({"boundary_price": 100.5}, "boundary_linked_fvgs"),
    ],
)
def test_builder_rejects_missing_acceptance_or_boundary_fvg_link(
    kwargs: dict[str, float],
    failed_stage: str,
) -> None:
    result = build_v07_scene_family_result(fixture_book(**kwargs))

    assert result.authorities == ()
    assert getattr(result.diagnostics, failed_stage) == 0


def test_execution_arms_share_one_scene_and_only_change_entry_contract() -> None:
    book = fixture_book()
    authority = build_v07_scene_family_result(book).authorities[0]

    limit = assemble_v07_opportunity(
        book,
        authority,
        costs=zero_costs(),
        entry_arm="first_return_limit",
    )
    next_open = assemble_v07_opportunity(
        book,
        authority,
        costs=zero_costs(),
        entry_arm=V07ExecutionArm.BOUNDARY_ACCEPT_NEXT_OPEN,
    )
    assert isinstance(limit, Opportunity)
    assert isinstance(next_open, Opportunity)
    assert limit.authority_id == next_open.authority_id == authority.authority_id
    assert limit.initial_stop == next_open.initial_stop == 98.7
    assert limit.target == next_open.target == authority.destination
    assert limit.planned_entry.price == 100.2
    assert limit.planned_entry.mode is EntryMode.LIMIT_FIRST_REVISIT
    assert next_open.planned_entry.price == authority.boundary_pivot.price
    assert next_open.planned_entry.mode is EntryMode.NEXT_BAR_OPEN

    with pytest.raises(ValueError):
        assemble_v07_opportunity(
            book,
            authority,
            costs=zero_costs(),
            entry_arm="unknown-arm",
        )


def test_next_open_arm_rejects_actual_fill_without_positive_fixed_target_net() -> None:
    book = fixture_book(next_open=102.85)
    authority = build_v07_scene_family_result(book).authorities[0]
    costs = CostConfig(0.001, 0.001, 0.001, 0.001)

    run = run_v07_historical_replay(
        book.frames[Timeframe.M5],
        symbol=book.symbol,
        tick_size=book.tick_size,
        equity=10_000,
        costs=costs,
        risk=RiskConfig(),
        entry_arm="boundary_accept_next_open",
        book=book,
        authorities=(authority,),
    )

    assert run.attempts == ()
    assert [item.reason for item in run.opportunity_rejections] == [
        "next_bar_open_not_cost_positive_to_fixed_target"
    ]


def test_next_open_arm_does_not_use_acceptance_close_as_extra_cost_filter() -> None:
    book = fixture_book(
        c_high=103.0,
        c_close=102.95,
        next_open=100.8,
    )
    authority = build_v07_scene_family_result(book).authorities[0]
    costs = CostConfig(0.001, 0.001, 0.001, 0.001)

    opportunity = assemble_v07_opportunity(
        book,
        authority,
        costs=costs,
        entry_arm="boundary_accept_next_open",
    )
    assert isinstance(opportunity, Opportunity)

    run = run_v07_historical_replay(
        book.frames[Timeframe.M5],
        symbol=book.symbol,
        tick_size=book.tick_size,
        equity=10_000,
        costs=costs,
        risk=RiskConfig(),
        entry_arm="boundary_accept_next_open",
        book=book,
        authorities=(authority,),
    )
    assert len(run.attempts) == 1
    assert run.opportunity_rejections == ()


def test_next_open_invalid_geometry_falls_back_to_same_cutoff_candidate() -> None:
    book = fixture_book(next_open=98.65)
    first = build_v07_scene_family_result(book).authorities[0]
    invalid = replace(first, authority_id="a-invalid-next-open")
    valid = replace(
        first,
        authority_id="z-valid-next-open",
        initial_stop=98.5,
    )

    run = run_v07_historical_replay(
        book.frames[Timeframe.M5],
        symbol=book.symbol,
        tick_size=book.tick_size,
        equity=10_000,
        costs=zero_costs(),
        risk=RiskConfig(),
        entry_arm="boundary_accept_next_open",
        book=book,
        authorities=(invalid, valid),
    )

    assert len(run.attempts) == 1
    assert run.attempts[0].authority_id == valid.authority_id
    assert [item.reason for item in run.opportunity_rejections] == [
        "next_bar_open_outside_trade_geometry"
    ]


def test_runner_uses_requested_arm_for_v07() -> None:
    book = fixture_book()
    authority = build_v07_scene_family_result(book).authorities[0]
    common = dict(
        candles_5m=book.frames[Timeframe.M5],
        symbol=book.symbol,
        tick_size=book.tick_size,
        equity=10_000,
        costs=zero_costs(),
        risk=RiskConfig(),
        book=book,
        authorities=(authority,),
    )

    next_open = run_v07_historical_replay(
        **common,
        entry_arm="boundary_accept_next_open",
    )
    limit = run_v07_historical_replay(
        **common,
        entry_arm="first_return_limit",
    )

    assert len(next_open.attempts) == len(limit.attempts) == 1
    assert next_open.attempts[0].intent.entry_mode is EntryMode.NEXT_BAR_OPEN
    assert limit.attempts[0].intent.entry_mode is EntryMode.LIMIT_FIRST_REVISIT
    assert next_open.attempts[0].result.events[0].price == 101.3


def _legacy_authority(at: pd.Timestamp) -> StructureFlipAuthority:
    first = bar("2024-12-31 23:00", 15, 100.5, 101.0, 99.0, 100.0)
    second = bar("2024-12-31 23:15", 15, 100.0, 102.0, 99.2, 101.5)
    location = OrderBlock(
        "legacy-location",
        "BTCUSDT",
        Timeframe.M15,
        ObKind.SIMPLE_2C,
        Side.LONG,
        (first, second),
        PriceZone(100.1, 100.2),
        second.close_time,
        99.0,
        98.9,
        102.0,
    )
    break_pivot = StrictPivot(
        "legacy-break-pivot",
        "BTCUSDT",
        Timeframe.M5,
        "high",
        100.0,
        ts("2025-01-01 00:20"),
        ts("2025-01-01 00:35"),
    )
    break_bar = bar("2025-01-01 00:40", 5, 100.5, 101.5, 100.4, 101.2)
    target = TargetCandidate(
        "legacy-target",
        "BTCUSDT",
        Side.LONG,
        "pivot",
        PriceZone(103.0, 103.0),
        at,
        "newer-unbroken-high",
    )
    return StructureFlipAuthority(
        "legacy-authority",
        "BTCUSDT",
        Side.LONG,
        location,
        None,
        break_pivot,
        break_bar,
        location.zone,
        at,
        99.0,
        98.9,
        102.0,
        target,
    )


def test_union_keeps_legacy_limit_mode_and_global_family_priority() -> None:
    book = fixture_book()
    v07 = build_v07_scene_family_result(book).authorities[0]
    legacy = _legacy_authority(v07.known_at)

    run = run_v07_historical_replay(
        book.frames[Timeframe.M5],
        symbol=book.symbol,
        tick_size=book.tick_size,
        equity=10_000,
        costs=zero_costs(),
        risk=RiskConfig(),
        entry_arm="boundary_accept_next_open",
        book=book,
        authorities=(v07, legacy),
    )

    assert len(run.attempts) == 1
    assert run.attempts[0].authority_id == legacy.authority_id
    assert run.attempts[0].intent.entry_mode is EntryMode.LIMIT_FIRST_REVISIT
