from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import (
    B1Subtype,
    FormationBar,
    PriceZone,
    SceneFamily,
    Side,
    TargetCandidate,
    Timeframe,
)
from ictbt.microstructure import (
    DualClockSceneKind,
    adapt_authority_to_dual_clock_scene,
)


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(opened: str, minutes: int, o: float, h: float, low: float, c: float):
    start = ts(opened)
    return FormationBar(
        open_time=start,
        close_time=start + pd.Timedelta(minutes=minutes),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=100.0,
    )


def target(side: Side) -> TargetCandidate:
    price = 102.0 if side is Side.LONG else 98.0
    return TargetCandidate(
        candidate_id=f"target-{side.value}",
        symbol="BTCUSDT",
        trade_side=side,
        kind="pivot",
        zone=PriceZone(price, price),
        known_at=ts("2025-01-01 00:00"),
        source_id=f"pivot-{side.value}",
    )


def test_v03_uses_liquidity_event_and_later_delivery_ob_clocks() -> None:
    event = SimpleNamespace(
        event_id="event-v03",
        event_time=ts("2025-01-01 00:00"),
        known_at=ts("2025-01-01 00:15"),
        node_price=100.0,
        subtype=B1Subtype.BREAK_RETEST,
    )
    owner = SimpleNamespace(
        formation_bars=(bar("2025-01-01 00:20", 5, 100.0, 100.5, 99.9, 100.4),)
    )
    confirmation = SimpleNamespace(
        authority_id="confirmation-v03",
        liquidity_event_id=event.event_id,
        liquidity_event_timeframe=Timeframe.M15,
        order_blocks=(owner,),
        known_at=ts("2025-01-01 00:25"),
    )
    authority = SimpleNamespace(
        authority_id="authority-v03",
        symbol="BTCUSDT",
        side=Side.LONG,
        scene_family=SceneFamily.A1_B1_CONFLUENCE,
        confirmation=confirmation,
        known_at=confirmation.known_at,
        initial_stop=99.0,
        destination=target(Side.LONG),
    )
    book = SimpleNamespace(liquidity_events={Timeframe.M15: (event,)})

    adapted = adapt_authority_to_dual_clock_scene(
        authority,
        book=book,
        tick_size=0.1,
    )

    assert adapted.scene.kind is DualClockSceneKind.BREAK_CONTINUATION
    assert adapted.scene.event_started_at == event.event_time
    assert adapted.scene.event_known_at == event.known_at
    assert adapted.scene.confirmation_started_at == ts("2025-01-01 00:20")
    assert adapted.scene.confirmation_known_at == confirmation.known_at
    assert adapted.source_event_id == event.event_id


def test_v05_never_reuses_owner_flow_before_event_is_known() -> None:
    event = SimpleNamespace(
        event_id="event-v05",
        event_time=ts("2025-01-01 01:00"),
        known_at=ts("2025-01-01 01:05"),
        node_price=100.0,
        subtype=B1Subtype.SWEEP_RECLAIM,
    )
    owner = SimpleNamespace(
        formation_bars=(
            bar("2025-01-01 01:00", 5, 99.8, 100.2, 99.5, 100.1),
            bar("2025-01-01 01:05", 5, 100.1, 100.5, 100.0, 100.4),
        )
    )
    authority = SimpleNamespace(
        authority_id="authority-v05",
        symbol="BTCUSDT",
        side=Side.LONG,
        scene_family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
        liquidity_event=event,
        delivery_ob=owner,
        delivery_fvg=None,
        delivery_root_id="delivery-v05",
        known_at=ts("2025-01-01 01:10"),
        initial_stop=99.0,
        destination=target(Side.LONG),
    )

    adapted = adapt_authority_to_dual_clock_scene(authority, tick_size=0.1)

    assert adapted.scene.kind is DualClockSceneKind.SWEEP_REVERSAL
    assert adapted.scene.confirmation_started_at == event.known_at
    assert adapted.scene.confirmation_known_at == authority.known_at


def test_v07_separates_break_bar_from_acceptance_bar() -> None:
    break_bar = bar("2025-01-01 02:00", 15, 99.8, 100.4, 99.7, 100.3)
    acceptance = bar("2025-01-01 02:15", 15, 100.3, 100.7, 100.2, 100.6)
    authority = SimpleNamespace(
        authority_id="authority-v07",
        symbol="BTCUSDT",
        side=Side.LONG,
        scene_family=SceneFamily.SR_FLIP_FVG,
        break_bar=break_bar,
        acceptance_bar=acceptance,
        boundary_pivot=SimpleNamespace(price=100.0),
        liquidity_event=SimpleNamespace(event_id="event-v07"),
        fvg=SimpleNamespace(fvg_id="fvg-v07"),
        known_at=acceptance.close_time,
        initial_stop=99.0,
        destination=target(Side.LONG),
    )

    adapted = adapt_authority_to_dual_clock_scene(authority, tick_size=0.1)

    assert adapted.scene.kind is DualClockSceneKind.BREAK_CONTINUATION
    assert adapted.scene.event_started_at == break_bar.open_time
    assert adapted.scene.event_known_at == break_bar.close_time
    assert adapted.scene.confirmation_started_at == acceptance.open_time
    assert adapted.scene.confirmation_known_at == acceptance.close_time


def test_v03_requires_feature_book_event_identity() -> None:
    authority = SimpleNamespace(
        authority_id="missing-book",
        symbol="BTCUSDT",
        side=Side.LONG,
        scene_family=SceneFamily.A1_B1_CONFLUENCE,
        destination=target(Side.LONG),
    )
    with pytest.raises(ValueError, match="requires its FeatureBook"):
        adapt_authority_to_dual_clock_scene(authority, tick_size=0.1)
