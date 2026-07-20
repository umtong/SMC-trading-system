from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ictbt.easychart_v0.domain import (
    B1Subtype,
    ConfirmationModel,
    FormationBar,
    LiquidityEvent,
    ObKind,
    OrderBlock,
    PriceZone,
    SceneFamily,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.strategy import (
    build_b1_confirmation,
    build_m15_liquidity_m5_delivery_confirmation,
    build_target_candidates,
    compose_a1_b1_confluence,
    confluence_entry_price,
    detect_b1_liquidity_events,
    match_m5_mss_displacement_pivot,
    select_initial_target,
    select_preferred_confluence,
    select_scene_initial_stop,
)


DELTA = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
}


def _block(
    ob_id: str,
    timeframe: Timeframe,
    *,
    known_at: str,
    zone: tuple[float, float],
    side: Side = Side.LONG,
) -> OrderBlock:
    end = pd.Timestamp(known_at)
    delta = DELTA[timeframe]
    first_open = end - 2 * delta
    if side is Side.LONG:
        bars = (
            FormationBar(first_open, first_open + delta, 102, 103, 98, 99, 10),
            FormationBar(first_open + delta, end, 98.5, 106, 97, 104, 20),
        )
        stop_extreme, initial_stop, impulse = 97.0, 96.5, 106.0
    else:
        bars = (
            FormationBar(first_open, first_open + delta, 98, 103, 97, 102, 10),
            FormationBar(first_open + delta, end, 102.5, 104, 94, 96, 20),
        )
        stop_extreme, initial_stop, impulse = 104.0, 104.5, 94.0
    return OrderBlock(
        ob_id=ob_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=ObKind.SIMPLE_2C,
        side=side,
        formation_bars=bars,
        zone=PriceZone(*zone),
        known_at=end,
        stop_extreme=stop_extreme,
        initial_stop=initial_stop,
        impulse_extreme=impulse,
    )


def _event(block: OrderBlock) -> LiquidityEvent:
    event_bar = block.formation_bars[-1]
    return LiquidityEvent(
        event_id=f"event:{block.ob_id}",
        symbol=block.symbol,
        timeframe=block.timeframe,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=block.side,
        node_id="h1-node",
        node_price=block.zone.low,
        event_time=event_bar.open_time,
        known_at=event_bar.close_time,
    )


def _confirmation(block: OrderBlock):
    return build_b1_confirmation(block, event=_event(block))


def test_liquidity_event_detector_finds_sweep_reclaim_and_break_retest() -> None:
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    bars = (
        FormationBar(
            start, start + pd.Timedelta(minutes=5), 99.8, 100.2, 99.8, 100, 1
        ),
        FormationBar(
            start + pd.Timedelta(minutes=5),
            start + pd.Timedelta(minutes=10),
            99,
            101,
            98.8,
            100.5,
            1,
        ),
        FormationBar(
            start + pd.Timedelta(minutes=10),
            start + pd.Timedelta(minutes=15),
            100.5,
            101,
            99.8,
            100.5,
            1,
        ),
    )
    high = StrictPivot(
        pivot_id="h1-high",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        kind="high",
        price=100,
        pivot_time=start - pd.Timedelta(hours=3),
        known_at=start - pd.Timedelta(hours=1),
    )
    low = StrictPivot(
        pivot_id="h1-low",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        kind="low",
        price=99.5,
        pivot_time=start - pd.Timedelta(hours=3),
        known_at=start - pd.Timedelta(hours=1),
    )

    events = detect_b1_liquidity_events(
        (high, low), timeframe=Timeframe.M5, bars=bars, tick_size=0.5
    )

    assert {event.subtype for event in events} == {
        B1Subtype.SWEEP_RECLAIM,
        B1Subtype.BREAK_RETEST,
    }
    extremes = {event.subtype: event.event_extreme for event in events}
    assert extremes[B1Subtype.SWEEP_RECLAIM] == 98.8
    assert extremes[B1Subtype.BREAK_RETEST] == 99.8


def test_liquidity_event_and_ob_form_one_b1_confirmation() -> None:
    block = _block(
        "m15-new", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100, 102)
    )

    confirmation = _confirmation(block)

    assert confirmation.subtype is B1Subtype.SWEEP_RECLAIM
    assert confirmation.liquidity_event_id == f"event:{block.ob_id}"
    assert confirmation.order_blocks == (block,)
    assert confirmation.timeframes == (Timeframe.M15,)


def test_m15_event_requires_the_m5_ob_to_own_a_confirmed_swing_break() -> None:
    block = _block(
        "m5-delivery",
        Timeframe.M5,
        known_at="2026-01-01T03:20:00Z",
        zone=(98.5, 99.0),
    )
    event = LiquidityEvent(
        event_id="m15-event",
        symbol=block.symbol,
        timeframe=Timeframe.M15,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=Side.LONG,
        node_id="h1-low",
        node_price=98.0,
        event_time=pd.Timestamp("2026-01-01T03:00:00Z"),
        known_at=pd.Timestamp("2026-01-01T03:15:00Z"),
        event_extreme=96.0,
    )
    pivot = StrictPivot(
        pivot_id="m5-high",
        symbol=block.symbol,
        timeframe=Timeframe.M5,
        kind="high",
        price=102.0,
        pivot_time=pd.Timestamp("2026-01-01T02:55:00Z"),
        known_at=pd.Timestamp("2026-01-01T03:10:00Z"),
    )

    confirmation = build_m15_liquidity_m5_delivery_confirmation(
        block, event=event, pivots=(pivot,), tick_size=0.5
    )

    assert confirmation is not None
    assert confirmation.confirmation_model is ConfirmationModel.M15_LIQUIDITY_M5_MSS_OB_V1
    assert confirmation.liquidity_event_timeframe is Timeframe.M15
    assert confirmation.timeframes == (Timeframe.M5,)
    assert confirmation.displacement_pivot_id == pivot.pivot_id
    assert confirmation.displacement_pivot_price == pivot.price
    assert confirmation.liquidity_event_extreme == 96.0
    h1_location = _block(
        "h1-location",
        Timeframe.H1,
        known_at="2026-01-01T03:00:00Z",
        zone=(97.0, 100.0),
    )
    authority = compose_a1_b1_confluence(
        h1_location, confirmation, tick_size=0.5
    )
    assert authority is not None
    assert authority.stop_extreme == 96.0
    assert authority.initial_stop == 95.5
    assert match_m5_mss_displacement_pivot(
        block, event=event, pivots=(replace(pivot, price=105.0),), tick_size=0.5
    ) is None


def test_confluence_requires_one_older_parent_ob_and_one_later_child_ob() -> None:
    h1 = _block(
        "h1-old", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 101)
    )
    m15 = _block(
        "m15-new", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100, 102)
    )

    authority = compose_a1_b1_confluence(
        h1, _confirmation(m15), tick_size=0.5
    )

    assert authority is not None
    assert authority.scene_family is SceneFamily.A1_B1_CONFLUENCE
    assert authority.location is h1
    assert authority.confirmation.order_blocks == (m15,)
    assert authority.zone == PriceZone(100, 101)
    assert confluence_entry_price(authority) == 101
    assert authority.initial_stop == 96.5
    assert authority.impulse_extreme == 106


def test_same_time_is_rejected_but_direct_5m_and_sequential_zones_are_allowed() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 101)
    )
    same_time = _block(
        "m15-same", Timeframe.M15, known_at="2026-01-01T03:00:00Z", zone=(100, 102)
    )
    m5_wrong_parent = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T03:05:00Z", zone=(100, 102)
    )
    narrow = _block(
        "m15-narrow", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100.8, 102)
    )

    assert compose_a1_b1_confluence(
        h1, _confirmation(same_time), tick_size=0.5
    ) is None
    direct = compose_a1_b1_confluence(
        h1, _confirmation(m5_wrong_parent), tick_size=0.5
    )
    sequential = compose_a1_b1_confluence(
        h1, _confirmation(narrow), tick_size=0.5
    )
    assert direct is not None
    assert direct.confirmation.timeframes == (Timeframe.M5,)
    assert sequential is not None
    assert sequential.zone == narrow.zone


def test_event_can_precede_the_first_b1_order_block() -> None:
    block = _block(
        "m5-after-event",
        Timeframe.M5,
        known_at="2026-01-01T03:15:00Z",
        zone=(100, 102),
    )
    event = LiquidityEvent(
        event_id="event-before-ob",
        symbol=block.symbol,
        timeframe=block.timeframe,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=block.side,
        node_id="h1-node",
        node_price=100,
        event_time=pd.Timestamp("2026-01-01T03:00:00Z"),
        known_at=pd.Timestamp("2026-01-01T03:05:00Z"),
    )

    confirmation = build_b1_confirmation(block, event=event)

    assert confirmation.known_at == block.known_at
    assert confirmation.liquidity_event_id == event.event_id


def test_h1_liquidity_pivot_can_be_the_a1_for_a_direct_5m_b1() -> None:
    block = _block(
        "m5-direct", Timeframe.M5, known_at="2026-01-01T03:05:00Z", zone=(100, 102)
    )
    confirmation = _confirmation(block)
    pivot = StrictPivot(
        pivot_id=confirmation.liquidity_node_id,
        symbol=block.symbol,
        timeframe=Timeframe.H1,
        kind="low",
        price=confirmation.liquidity_node_price,
        pivot_time=pd.Timestamp("2026-01-01T01:00:00Z"),
        known_at=pd.Timestamp("2026-01-01T02:00:00Z"),
    )

    authority = compose_a1_b1_confluence(pivot, confirmation, tick_size=0.5)

    assert authority is not None
    assert authority.location is pivot
    assert authority.zone == block.zone
    assert authority.initial_stop == block.initial_stop


def test_h1_a1_wick_does_not_widen_the_m15_b1_stop() -> None:
    h1 = _block(
        "h1-wide", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 103)
    )
    wide_bars = tuple(
        replace(bar, low=90.0 if index == 0 else 89.0)
        for index, bar in enumerate(h1.formation_bars)
    )
    h1 = replace(
        h1,
        formation_bars=wide_bars,
        stop_extreme=89.0,
        initial_stop=88.5,
    )
    m15 = _block(
        "m15-execution",
        Timeframe.M15,
        known_at="2026-01-01T03:15:00Z",
        zone=(100, 102),
    )

    authority = compose_a1_b1_confluence(h1, _confirmation(m15), tick_size=0.5)

    assert authority is not None
    assert authority.initial_stop == m15.initial_stop


def test_15m_plus_5m_scene_has_priority_over_1h_plus_15m() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 103)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100, 102)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T03:20:00Z", zone=(100.5, 101.5)
    )
    higher = compose_a1_b1_confluence(
        h1, _confirmation(m15), tick_size=0.5
    )
    lower = compose_a1_b1_confluence(
        m15, _confirmation(m5), tick_size=0.5
    )
    assert higher is not None and lower is not None

    selected = select_preferred_confluence(
        (higher, lower), symbol="BTCUSDT", side=Side.LONG
    )

    assert selected is lower
    assert selected.zone == PriceZone(100.5, 101.5)


def test_execution_impulse_extreme_has_no_standalone_target_authority() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 101)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100, 102)
    )
    authority = compose_a1_b1_confluence(
        h1, _confirmation(m15), tick_size=0.5
    )
    assert authority is not None



def test_independent_pivot_at_impulse_price_retains_target_authority() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T03:00:00Z", zone=(99, 101)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T03:15:00Z", zone=(100, 102)
    )
    authority = compose_a1_b1_confluence(
        h1, _confirmation(m15), tick_size=0.5
    )
    assert authority is not None
    pivot = StrictPivot(
        pivot_id="independent-high",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        kind="high",
        price=authority.impulse_extreme,
        pivot_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        known_at=pd.Timestamp("2026-01-01T01:00:00Z"),
    )

    candidates = build_target_candidates(authority, pivots=(pivot,))

    assert len(candidates) == 1
    assert candidates[0].kind == "pivot"
    assert candidates[0].order_price == authority.impulse_extreme
    assert build_target_candidates(authority) == ()


def test_execution_ob_owns_stop_when_formation_wicks_include_event_extreme() -> None:
    block = _block(
        "m5-execution",
        Timeframe.M5,
        known_at="2026-01-01T03:05:00Z",
        zone=(100, 102),
    )
    selected = select_scene_initial_stop(
        block, event_extreme=98.0, side=Side.LONG, tick_size=0.5
    )
    assert selected.owner == "execution_ob"
    assert selected.stop_extreme == 97.0
    assert selected.initial_stop == 96.5


def test_liquidity_event_owns_stop_when_extreme_is_outside_execution_wicks() -> None:
    block = _block(
        "m5-execution",
        Timeframe.M5,
        known_at="2026-01-01T03:05:00Z",
        zone=(100, 102),
    )
    selected = select_scene_initial_stop(
        block, event_extreme=95.0, side=Side.LONG, tick_size=0.5
    )
    assert selected.owner == "liquidity_event"
    assert selected.stop_extreme == 95.0
    assert selected.initial_stop == 94.5


def test_short_scene_stop_is_one_tick_above_owning_boundary() -> None:
    block = _block(
        "m5-short",
        Timeframe.M5,
        known_at="2026-01-01T03:05:00Z",
        zone=(100, 102),
        side=Side.SHORT,
    )
    owned = select_scene_initial_stop(
        block, event_extreme=103.5, side=Side.SHORT, tick_size=0.5
    )
    event_owned = select_scene_initial_stop(
        block, event_extreme=106.0, side=Side.SHORT, tick_size=0.5
    )
    assert (owned.owner, owned.stop_extreme, owned.initial_stop) == (
        "execution_ob", 104.0, 104.5
    )
    assert (event_owned.owner, event_owned.stop_extreme, event_owned.initial_stop) == (
        "liquidity_event", 106.0, 106.5
    )

def test_target_zone_crossing_entry_blocks_farther_target() -> None:
    candidates = (
        TargetCandidate(
            candidate_id="near-crossing-zone",
            symbol="BTCUSDT",
            trade_side=Side.SHORT,
            kind="fvg",
            zone=PriceZone(94.0, 101.0),
            known_at=pd.Timestamp("2026-01-01T00:00:00Z"),
            source_id="near-crossing-zone",
        ),
        TargetCandidate(
            candidate_id="impulse-inside-zone",
            symbol="BTCUSDT",
            trade_side=Side.SHORT,
            kind="impulse",
            zone=PriceZone(96.0, 96.0),
            known_at=pd.Timestamp("2026-01-01T00:00:00Z"),
            source_id="impulse-inside-zone",
        ),
        TargetCandidate(
            candidate_id="far-target",
            symbol="BTCUSDT",
            trade_side=Side.SHORT,
            kind="pivot",
            zone=PriceZone(80.0, 80.0),
            known_at=pd.Timestamp("2026-01-01T00:00:00Z"),
            source_id="far-target",
        ),
    )

    selection = select_initial_target(
        candidates,
        side=Side.SHORT,
        entry_price=100.0,
        tick_size=0.5,
    )

    assert selection.target is None
    assert selection.rejection_reason == "target_space_conflict"


def test_short_uses_overlap_low_and_far_wick_stop() -> None:
    h1 = _block(
        "h1-short",
        Timeframe.H1,
        known_at="2026-01-01T03:00:00Z",
        zone=(99, 103),
        side=Side.SHORT,
    )
    m15 = _block(
        "m15-short",
        Timeframe.M15,
        known_at="2026-01-01T03:15:00Z",
        zone=(100, 102),
        side=Side.SHORT,
    )

    authority = compose_a1_b1_confluence(
        h1, _confirmation(m15), tick_size=0.5
    )

    assert authority is not None
    assert authority.zone == PriceZone(100, 102)
    assert confluence_entry_price(authority) == 100
    assert authority.initial_stop == 104.5
    assert authority.impulse_extreme == 94


