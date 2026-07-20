from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.v08 as v08
from ictbt.easychart_v0.domain import (
    B1Subtype,
    FairValueGap,
    FormationBar,
    LiquidityEvent,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.pipeline import FeatureBook, StructureState
from ictbt.easychart_v0.v07 import SrFlipFvgAuthority


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(
    opened: str,
    minutes: int,
    o: float,
    high: float,
    low: float,
    close: float,
) -> FormationBar:
    start = ts(opened)
    return FormationBar(
        open_time=start,
        close_time=start + pd.Timedelta(minutes=minutes),
        open=o,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def prior_m15_frame() -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    opened: list[pd.Timestamp] = []
    start = ts("2025-01-01 00:00")
    for index in range(12):
        opened.append(start + pd.Timedelta(minutes=15 * index))
        rows.append(
            {
                "open": 98.5,
                "high": 99.2,
                "low": 98.2,
                "close": 98.8,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(opened, name="open_time"),
        dtype=float,
    )


def pivot(
    pivot_id: str,
    timeframe: Timeframe,
    kind: str,
    price: float,
    pivot_time: str,
    known_at: str,
) -> StrictPivot:
    return StrictPivot(
        pivot_id=pivot_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=kind,  # type: ignore[arg-type]
        price=price,
        pivot_time=ts(pivot_time),
        known_at=ts(known_at),
    )


def base_authority(
    *,
    boundary_timeframe: Timeframe = Timeframe.M15,
    initial_stop: float = 98.7,
) -> SrFlipFvgAuthority:
    a_bar = bar("2025-01-01 03:00", 15, 99.2, 99.8, 98.8, 99.4)
    b_bar = bar("2025-01-01 03:15", 15, 99.4, 103.0, 99.2, 102.5)
    c_bar = bar("2025-01-01 03:30", 15, 102.2, 103.1, 100.2, 102.0)
    boundary = pivot(
        "boundary",
        boundary_timeframe,
        "high",
        100.0,
        "2024-12-31 18:00",
        "2024-12-31 20:00",
    )
    gap = FairValueGap(
        fvg_id="execution-fvg",
        symbol="BTCUSDT",
        timeframe=Timeframe.M15,
        side=Side.LONG,
        formation_bars=(a_bar, b_bar, c_bar),
        zone=PriceZone(99.8, 100.2),
        known_at=c_bar.close_time,
    )
    event = LiquidityEvent(
        event_id="boundary-break",
        symbol="BTCUSDT",
        timeframe=Timeframe.M15,
        subtype=B1Subtype.BREAK_RETEST,
        side=Side.LONG,
        node_id=boundary.pivot_id,
        node_price=boundary.price,
        event_time=b_bar.open_time,
        known_at=c_bar.close_time,
        event_extreme=98.8,
    )
    placeholder = TargetCandidate(
        candidate_id="legacy-fvg-target",
        symbol="BTCUSDT",
        trade_side=Side.LONG,
        kind="fvg",
        zone=PriceZone(101.0, 101.2),
        known_at=ts("2024-12-31 20:00"),
        source_id="legacy-fvg",
    )
    return SrFlipFvgAuthority(
        authority_id="v07-base",
        scene_root_id="v07-root",
        symbol="BTCUSDT",
        side=Side.LONG,
        boundary_pivot=boundary,
        liquidity_event=event,
        fvg=gap,
        break_bar=b_bar,
        acceptance_bar=c_bar,
        zone=PriceZone(100.0, 100.0),
        known_at=c_bar.close_time,
        stop_extreme=98.8,
        initial_stop=initial_stop,
        impulse_extreme=103.1,
        destination=placeholder,
    )


def feature_book(
    *,
    target_price: float | None = 104.0,
) -> FeatureBook:
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M15] = prior_m15_frame()
    pivots = {timeframe: () for timeframe in Timeframe}
    if target_price is not None:
        pivots[Timeframe.H4] = (
            pivot(
                "external-liquidity",
                Timeframe.H4,
                "high",
                target_price,
                "2024-12-31 12:00",
                "2024-12-31 20:00",
            ),
        )
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots=pivots,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def costs() -> CostConfig:
    return CostConfig(
        entry_fee_rate=0.0002,
        stop_fee_rate=0.0006,
        target_fee_rate=0.0002,
        volume_exit_fee_rate=0.0006,
        stop_slippage_bps=2.0,
        volume_exit_slippage_bps=2.0,
    )


def patch_base(
    monkeypatch,
    authority: SrFlipFvgAuthority,
    *,
    h1: StructureState,
    h4: StructureState,
) -> None:
    monkeypatch.setattr(
        v08,
        "build_v07_scene_family_result",
        lambda _book: SimpleNamespace(authorities=(authority,)),
    )
    monkeypatch.setattr(
        v08,
        "structure_snapshot",
        lambda _book, as_of: SimpleNamespace(h1=h1, h4=h4),
    )


def test_v08_retargets_to_preexisting_h4_liquidity(monkeypatch) -> None:
    authority = base_authority()
    patch_base(
        monkeypatch,
        authority,
        h1=StructureState.UP,
        h4=StructureState.RANGE,
    )

    result = v08.build_v08_scene_family_result(feature_book(), costs=costs())

    assert len(result.authorities) == 1
    selected = result.authorities[0]
    assert selected.destination.kind == "pivot"
    assert selected.destination.source_id == "external-liquidity"
    assert selected.authority_id.startswith("v08-htf-liquidity-delivery:")
    assert result.diagnostics.trend_continuation_authorities == 1


def test_v08_rejects_countertrend_and_fvg_only_terminal_target(monkeypatch) -> None:
    authority = base_authority()
    patch_base(
        monkeypatch,
        authority,
        h1=StructureState.DOWN,
        h4=StructureState.DOWN,
    )
    countertrend = v08.build_v08_scene_family_result(feature_book(), costs=costs())
    assert countertrend.authorities == ()
    assert countertrend.diagnostics.htf_context_rejections == 1

    patch_base(
        monkeypatch,
        authority,
        h1=StructureState.UP,
        h4=StructureState.RANGE,
    )
    no_external = v08.build_v08_scene_family_result(
        feature_book(target_price=None),
        costs=costs(),
    )
    assert no_external.authorities == ()
    assert no_external.diagnostics.external_liquidity_missing == 1


def test_v08_range_expansion_requires_h1_boundary(monkeypatch) -> None:
    m15_authority = base_authority(boundary_timeframe=Timeframe.M15)
    patch_base(
        monkeypatch,
        m15_authority,
        h1=StructureState.RANGE,
        h4=StructureState.RANGE,
    )
    rejected = v08.build_v08_scene_family_result(feature_book(), costs=costs())
    assert rejected.authorities == ()

    h1_authority = base_authority(boundary_timeframe=Timeframe.H1)
    patch_base(
        monkeypatch,
        h1_authority,
        h1=StructureState.RANGE,
        h4=StructureState.RANGE,
    )
    accepted = v08.build_v08_scene_family_result(feature_book(), costs=costs())
    assert accepted.diagnostics.h1_range_expansion_authorities == 1


def test_v08_rejects_weak_displacement_small_target_and_excess_exposure(
    monkeypatch,
) -> None:
    authority = base_authority()
    patch_base(
        monkeypatch,
        authority,
        h1=StructureState.UP,
        h4=StructureState.RANGE,
    )
    weak = v08.build_v08_scene_family_result(
        feature_book(),
        costs=costs(),
        policy=v08.V08Policy(minimum_displacement_range_multiple=5.0),
    )
    assert weak.diagnostics.displacement_rejections == 1

    small_target = v08.build_v08_scene_family_result(
        feature_book(target_price=100.8),
        costs=costs(),
    )
    assert small_target.diagnostics.target_space_rejections == 1

    narrow = replace(authority, initial_stop=99.95)
    patch_base(
        monkeypatch,
        narrow,
        h1=StructureState.UP,
        h4=StructureState.RANGE,
    )
    exposure = v08.build_v08_scene_family_result(
        feature_book(),
        costs=costs(),
        policy=v08.V08Policy(maximum_notional_to_equity=2.0),
    )
    assert exposure.diagnostics.exposure_rejections == 1
