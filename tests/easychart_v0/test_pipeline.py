from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ictbt.easychart_v0.domain import (
    B1Subtype,
    EntryMode,
    FormationBar,
    LiquidityEvent,
    ObKind,
    OrderBlock,
    PriceZone,
    SceneFamily,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.pipeline import (
    FeatureBook,
    Opportunity,
    assemble_confluence_opportunities,
    build_confluence_authorities,
    enumerate_b1_confirmations,
    select_current_confluence,
)


DELTA = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
}


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def _block(
    ob_id: str,
    timeframe: Timeframe,
    *,
    known_at: str,
    zone: tuple[float, float],
) -> OrderBlock:
    end = pd.Timestamp(known_at)
    delta = DELTA[timeframe]
    first = end - 2 * delta
    bars = (
        FormationBar(first, first + delta, 102, 103, 98, 99, 10),
        FormationBar(first + delta, end, 98.5, 106, 97, 104, 20),
    )
    return OrderBlock(
        ob_id=ob_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=ObKind.SIMPLE_2C,
        side=Side.LONG,
        formation_bars=bars,
        zone=PriceZone(*zone),
        known_at=end,
        stop_extreme=97,
        initial_stop=96.5,
        impulse_extreme=106,
    )


def _pivot(pivot_id: str, kind: str, price: float, hour: int) -> StrictPivot:
    opened = pd.Timestamp(f"2026-01-01T{hour:02d}:00:00Z")
    return StrictPivot(
        pivot_id=pivot_id,
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        kind=kind,
        price=price,
        pivot_time=opened,
        known_at=opened + pd.Timedelta(hours=1),
    )


def _m15_event(block: OrderBlock) -> LiquidityEvent:
    displacement = block.formation_bars[-1]
    return LiquidityEvent(
        event_id=f"m15-event:{block.ob_id}",
        symbol=block.symbol,
        timeframe=Timeframe.M15,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=block.side,
        node_id="low-2",
        node_price=101.0,
        event_time=displacement.open_time - pd.Timedelta(minutes=15),
        known_at=displacement.open_time,
    )


def _book(
    *blocks: OrderBlock,
    m5_frame: pd.DataFrame | None = None,
    include_liquidity_events: bool = True,
) -> FeatureBook:
    frames = {timeframe: _empty_frame() for timeframe in Timeframe}
    if m5_frame is not None:
        frames[Timeframe.M5] = m5_frame
    order_blocks = {
        timeframe: tuple(block for block in blocks if block.timeframe is timeframe)
        for timeframe in Timeframe
    }
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.H1] = (
        _pivot("high-1", "high", 108, 0),
        _pivot("low-1", "low", 90, 1),
        _pivot("high-2", "high", 112, 2),
        _pivot("low-2", "low", 101, 3),
    )
    pivots[Timeframe.M5] = tuple(
        StrictPivot(
            pivot_id=f"m5-mss:{block.ob_id}",
            symbol=block.symbol,
            timeframe=Timeframe.M5,
            kind="high",
            price=102.0,
            pivot_time=block.formation_bars[-1].open_time - pd.Timedelta(minutes=10),
            known_at=block.formation_bars[-1].open_time,
        )
        for block in blocks
        if block.timeframe is Timeframe.M5
    )
    liquidity_events = {
        Timeframe.M5: (),
        Timeframe.M15: tuple(
            _m15_event(block)
            for block in blocks
            if include_liquidity_events and block.timeframe is Timeframe.M5
        ),
    }
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.5,
        frames=frames,
        order_blocks=order_blocks,
        pivots=pivots,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events=liquidity_events,
    )


def test_h1_location_alone_is_not_an_opportunity_but_mtf_delivery_can_confirm() -> None:
    h1 = _block(
        "h1-old", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5-delivery", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100, 102)
    )

    assert build_confluence_authorities(
        _book(h1), as_of="2026-01-01T05:00:00Z"
    ) == ()
    confirmations = enumerate_b1_confirmations(
        _book(m5), as_of="2026-01-01T05:00:00Z"
    )
    assert len(confirmations) == 1
    assert confirmations[0].liquidity_event_timeframe is Timeframe.M15
    assert confirmations[0].timeframes == (Timeframe.M5,)
def test_plain_ob_without_a_liquidity_event_is_not_b1() -> None:
    h1 = _block(
        "h1-old", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5-delivery", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100, 102)
    )
    book = _book(h1, m5, include_liquidity_events=False)

    assert enumerate_b1_confirmations(
        book, as_of="2026-01-01T04:20:00Z"
    ) == ()
    assert assemble_confluence_opportunities(
        book, as_of="2026-01-01T04:20:00Z"
    ) == ()

def test_h1_context_and_m15_event_m5_delivery_create_one_opportunity() -> None:
    h1 = _block(
        "h1-old", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5-delivery", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100, 102)
    )
    book = _book(h1, m5)

    results = assemble_confluence_opportunities(
        book, as_of="2026-01-01T04:20:00Z"
    )

    assert len(results) == 1
    opportunity = results[0]
    assert isinstance(opportunity, Opportunity)
    assert opportunity.scene_family is SceneFamily.A1_B1_CONFLUENCE
    assert opportunity.authority.location is h1
    assert opportunity.authority.confirmation.order_blocks == (m5,)
    assert opportunity.authority.confirmation.liquidity_event_timeframe is Timeframe.M15
    assert opportunity.authority.confirmation.displacement_pivot_id == "m5-mss:m5-delivery"
    assert opportunity.planned_entry.price == 102
    assert opportunity.planned_entry.mode.value == "next_bar_open"
    assert opportunity.planned_entry.ob_causal_state.value == "event_created"
    assert opportunity.initial_stop == 96.5

def test_order_block_location_must_exist_before_the_m15_event() -> None:
    h1_existing = _block(
        "h1-existing", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    h1_after_event = _block(
        "h1-after-event", Timeframe.H1, known_at="2026-01-01T04:17:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5-delivery", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100, 102)
    )

    authorities = build_confluence_authorities(
        _book(h1_existing, h1_after_event, m5),
        as_of="2026-01-01T04:20:00Z",
    )
    ob_locations = {
        authority.location.ob_id
        for authority in authorities
        if isinstance(authority.location, OrderBlock)
    }

    assert "h1-existing" in ob_locations
    assert "h1-after-event" not in ob_locations


def test_m15_event_timeframe_pivot_remains_a_same_scene_target() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5-delivery", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100, 102)
    )
    book = _book(h1, m5)
    m15_target = StrictPivot(
        pivot_id="m15-same-scene-high",
        symbol=book.symbol,
        timeframe=Timeframe.M15,
        kind="high",
        price=104.0,
        pivot_time=pd.Timestamp("2026-01-01T03:45:00Z"),
        known_at=pd.Timestamp("2026-01-01T04:15:00Z"),
    )
    pivots = dict(book.pivots)
    pivots[Timeframe.M15] = (m15_target,)
    book = replace(book, pivots=pivots)

    results = assemble_confluence_opportunities(
        book, as_of="2026-01-01T04:20:00Z"
    )
    opportunity = next(
        item
        for item in results
        if isinstance(item, Opportunity) and item.authority.location is h1
    )

    assert opportunity.target.source_id == m15_target.pivot_id

def test_15m_plus_5m_is_selected_when_both_valid_pairs_exist() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T04:15:00Z", zone=(100, 102)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100.5, 101.5)
    )

    selected = select_current_confluence(
        _book(h1, m15, m5), as_of="2026-01-01T04:20:00Z"
    )

    assert selected is not None
    assert selected.location is m15
    assert selected.confirmation.order_blocks == (m5,)
    assert selected.zone == PriceZone(100.5, 101.5)


def test_event_created_next_open_scene_expires_after_its_birth_close() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100.5, 101.5)
    )
    book = _book(h1, m5)

    assert select_current_confluence(book, as_of="2026-01-01T04:20:00Z") is not None
    assert select_current_confluence(book, as_of="2026-01-01T04:25:00Z") is None


def test_event_created_first_revisit_arm_remains_available_until_touch() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100.5, 101.5)
    )
    book = _book(h1, m5)

    selected = select_current_confluence(
        book,
        as_of="2026-01-01T04:25:00Z",
        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
    )
    results = assemble_confluence_opportunities(
        book,
        as_of="2026-01-01T04:25:00Z",
        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
    )

    assert selected is not None
    assert len(results) == 1
    assert isinstance(results[0], Opportunity)
    assert results[0].planned_entry.mode is EntryMode.LIMIT_FIRST_REVISIT


def test_submitted_preferred_scene_does_not_hide_an_available_scene() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T04:15:00Z", zone=(100, 102)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100.5, 101.5)
    )
    book = _book(h1, m15, m5)
    preferred = select_current_confluence(book, as_of="2026-01-01T04:20:00Z")
    assert preferred is not None

    fallback = select_current_confluence(
        book,
        as_of="2026-01-01T04:20:00Z",
        excluded_authority_ids=frozenset({preferred.authority_id}),
    )

    assert fallback is not None
    assert fallback.authority_id != preferred.authority_id
    assert fallback.location is h1
    assert fallback.confirmation.order_blocks == (m5,)


def test_h1_liquidity_location_can_confirm_a_direct_5m_route() -> None:
    m5 = _block(
        "m5-direct", Timeframe.M5, known_at="2026-01-01T04:05:00Z", zone=(93, 95)
    )
    book = _book(m5)

    authorities = build_confluence_authorities(
        book, as_of="2026-01-01T04:05:00Z"
    )

    assert len(authorities) == 1
    authority = authorities[0]
    assert isinstance(authority.location, StrictPivot)
    assert authority.location.pivot_id == "low-2"
    assert authority.confirmation.timeframes == (Timeframe.M5,)
    assert authority.confirmation.liquidity_event_timeframe is Timeframe.M15

def test_consumed_delivery_zone_does_not_create_a_duplicate_location_fallback() -> None:
    h1 = _block(
        "h1", Timeframe.H1, known_at="2026-01-01T04:00:00Z", zone=(99, 103)
    )
    m15 = _block(
        "m15", Timeframe.M15, known_at="2026-01-01T04:15:00Z", zone=(100, 102)
    )
    m5 = _block(
        "m5", Timeframe.M5, known_at="2026-01-01T04:20:00Z", zone=(100.5, 101.5)
    )
    revisit = pd.DataFrame(
        [[102, 103, 101, 102.5, 50]],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01T04:20:00Z")], name="open_time"),
    )

    results = assemble_confluence_opportunities(
        _book(h1, m15, m5, m5_frame=revisit),
        as_of="2026-01-01T04:25:00Z",
    )

    assert results == ()


