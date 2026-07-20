from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import (
    FormationBar,
    LiquidityEvent,
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
from ictbt.easychart_v0.execution import CostConfig, RiskConfig, build_confluence_intent
from ictbt.easychart_v0.pipeline import FeatureBook, Opportunity
from ictbt.easychart_v0.v04 import (
    _m5_sweep_episode_is_valid,
    assemble_v04_opportunity,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(opened: str, minutes: int, o: float, h: float, l: float, c: float) -> FormationBar:
    start = ts(opened)
    return FormationBar(start, start + pd.Timedelta(minutes=minutes), o, h, l, c, 100.0)


def block(
    block_id: str,
    timeframe: Timeframe,
    side: Side,
    first: FormationBar,
    second: FormationBar,
    zone: PriceZone,
    stop_extreme: float,
    initial_stop: float,
    impulse: float,
) -> OrderBlock:
    return OrderBlock(
        block_id,
        "BTCUSDT",
        timeframe,
        ObKind.SIMPLE_2C,
        side,
        (first, second),
        zone,
        second.close_time,
        stop_extreme,
        initial_stop,
        impulse,
    )


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def authority() -> StructureFlipAuthority:
    location = block(
        "m15-location",
        Timeframe.M15,
        Side.LONG,
        bar("2025-01-01 00:00", 15, 101, 102, 99, 100),
        bar("2025-01-01 00:15", 15, 100, 104, 100, 103),
        PriceZone(100, 101),
        99,
        98.5,
        104,
    )
    refinement = block(
        "m5-refinement",
        Timeframe.M5,
        Side.LONG,
        bar("2025-01-01 00:45", 5, 101, 101.5, 100.2, 100.5),
        bar("2025-01-01 00:50", 5, 100.4, 102, 100.4, 101.8),
        PriceZone(100.4, 100.5),
        100.2,
        100.1,
        102,
    )
    pivot = StrictPivot(
        "m5-high",
        "BTCUSDT",
        Timeframe.M5,
        "high",
        102,
        ts("2025-01-01 00:35"),
        ts("2025-01-01 00:50"),
    )
    break_bar = bar("2025-01-01 01:00", 5, 101.8, 104, 101.5, 103.5)
    target = TargetCandidate(
        "departure",
        "BTCUSDT",
        Side.LONG,
        "impulse",
        PriceZone(104, 104),
        break_bar.close_time,
        refinement.ob_id,
    )
    return StructureFlipAuthority(
        "structure-scene",
        "BTCUSDT",
        Side.LONG,
        location,
        refinement,
        pivot,
        break_bar,
        PriceZone(100.4, 100.5),
        break_bar.close_time,
        refinement.stop_extreme,
        refinement.initial_stop,
        104,
        target,
        "m5-sweep",
        "m15-low",
    )


def test_structure_scene_uses_preexisting_first_revisit_contract() -> None:
    item = authority()
    assert item.scene_family is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
    assert item.execution_ob.ob_id == "m5-refinement"
    assert item.entry_mode.value == "limit_first_revisit"


def test_structure_scene_uses_its_frozen_departure_target() -> None:
    item = authority()
    book = FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames={timeframe: empty_frame() for timeframe in Timeframe},
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots={timeframe: () for timeframe in Timeframe},
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )
    result = assemble_v04_opportunity(
        book,
        item,
        costs=CostConfig(0, 0, 0, 0, 0, 0),
    )
    assert isinstance(result, Opportunity)
    assert result.planned_entry.price == 100.5
    assert result.initial_stop == 100.1
    assert result.target.order_price == 104


def test_new_scene_family_is_executable_with_the_common_risk_engine() -> None:
    intent = build_confluence_intent(
        order_id="order",
        source_id="scene",
        symbol="BTCUSDT",
        side=Side.LONG,
        created_at=ts("2025-01-01 01:05"),
        entry_reference=100.5,
        initial_stop=100.1,
        initial_target=104,
        equity=10_000,
        costs=CostConfig(0, 0, 0, 0, 0, 0),
        risk=RiskConfig(risk_fraction=0.03, quantity_step=0.001),
        scene_family=SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST,
    )
    assert intent.risk_budget == 300
    assert intent.scene_family is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST


def test_m5_sweep_episode_ends_after_opposite_close_through_node() -> None:
    frame = pd.DataFrame(
        [
            (100, 102, 99, 101, 100),
            (101, 101.5, 98, 98.5, 100),
        ],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.date_range("2025-01-01 00:00", periods=2, freq="5min", tz="UTC"),
        dtype=float,
    )
    book = FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.5,
        frames={
            Timeframe.M5: frame,
            Timeframe.M15: empty_frame(),
            Timeframe.H1: empty_frame(),
            Timeframe.H4: empty_frame(),
        },
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots={timeframe: () for timeframe in Timeframe},
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )
    event = LiquidityEvent(
        "sweep",
        "BTCUSDT",
        Timeframe.M5,
        "sweep_reclaim",
        Side.LONG,
        "m15-low",
        100,
        ts("2025-01-01 00:00"),
        ts("2025-01-01 00:05"),
        99,
    )
    assert not _m5_sweep_episode_is_valid(
        book,
        event,
        until=ts("2025-01-01 00:10"),
    )
