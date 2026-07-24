from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import (
    B1Subtype,
    PriceZone,
    SceneFamily,
    Side,
    TargetCandidate,
)
from ictbt.microstructure import FlowSceneKind, adapt_authority_to_flow_scene


KNOWN = pd.Timestamp("2025-01-01T00:45:00Z")


def target(side: Side, price: float) -> TargetCandidate:
    return TargetCandidate(
        candidate_id=f"target-{side.value}",
        symbol="BTCUSDT",
        trade_side=side,
        kind="pivot",
        zone=PriceZone(price, price),
        known_at=KNOWN - pd.Timedelta(hours=1),
        source_id=f"pivot-{price}",
    )


def base(
    *,
    family: SceneFamily,
    side: Side,
    destination: TargetCandidate | None,
) -> dict[str, object]:
    return {
        "authority_id": f"authority-{family.value}",
        "symbol": "BTCUSDT",
        "side": side,
        "scene_family": family,
        "known_at": KNOWN,
        "initial_stop": 90.0 if side is Side.LONG else 110.0,
        "destination": destination,
    }


def test_v03_break_retest_preserves_node_clock_stop_and_target() -> None:
    authority = SimpleNamespace(
        **base(
            family=SceneFamily.A1_B1_CONFLUENCE,
            side=Side.LONG,
            destination=target(Side.LONG, 110.0),
        ),
        confirmation=SimpleNamespace(
            liquidity_node_price=100.0,
            subtype=B1Subtype.BREAK_RETEST,
        ),
    )
    adapted = adapt_authority_to_flow_scene(authority, tick_size=0.1)

    assert adapted.scene.kind is FlowSceneKind.BREAK_ACCEPTANCE
    assert adapted.scene.node_price == 100.0
    assert adapted.scene.known_at == KNOWN
    assert adapted.scene.initial_stop == 90.0
    assert adapted.scene.initial_target == 110.0
    assert adapted.source_target_id == "pivot-110.0"


def test_v03_sweep_and_v05_delivery_map_to_sweep_reclaim() -> None:
    v03 = SimpleNamespace(
        **base(
            family=SceneFamily.A1_B1_CONFLUENCE,
            side=Side.SHORT,
            destination=target(Side.SHORT, 90.0),
        ),
        confirmation=SimpleNamespace(
            liquidity_node_price=100.0,
            subtype=B1Subtype.SWEEP_RECLAIM,
        ),
    )
    v05 = SimpleNamespace(
        **base(
            family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
            side=Side.SHORT,
            destination=target(Side.SHORT, 90.0),
        ),
        liquidity_event=SimpleNamespace(node_price=100.0),
    )

    assert adapt_authority_to_flow_scene(v03, tick_size=0.1).scene.kind is FlowSceneKind.SWEEP_RECLAIM
    assert adapt_authority_to_flow_scene(v05, tick_size=0.1).scene.kind is FlowSceneKind.SWEEP_RECLAIM


def test_v07_boundary_acceptance_uses_boundary_pivot() -> None:
    authority = SimpleNamespace(
        **base(
            family=SceneFamily.SR_FLIP_FVG,
            side=Side.LONG,
            destination=target(Side.LONG, 110.0),
        ),
        boundary_pivot=SimpleNamespace(price=100.0),
    )
    adapted = adapt_authority_to_flow_scene(authority, tick_size=0.1)

    assert adapted.scene.kind is FlowSceneKind.BREAK_ACCEPTANCE
    assert adapted.scene.node_price == 100.0
    assert adapted.source_scene_family is SceneFamily.SR_FLIP_FVG


def test_dynamic_v03_requires_existing_selector_to_freeze_target() -> None:
    authority = SimpleNamespace(
        **base(
            family=SceneFamily.A1_B1_CONFLUENCE,
            side=Side.LONG,
            destination=None,
        ),
        confirmation=SimpleNamespace(
            liquidity_node_price=100.0,
            subtype=B1Subtype.BREAK_RETEST,
        ),
    )
    with pytest.raises(ValueError, match="frozen TargetCandidate"):
        adapt_authority_to_flow_scene(authority, tick_size=0.1)

    adapted = adapt_authority_to_flow_scene(
        authority,
        tick_size=0.1,
        destination=target(Side.LONG, 110.0),
    )
    assert adapted.scene.initial_target == 110.0


def test_unregistered_scene_family_is_rejected() -> None:
    authority = SimpleNamespace(
        **base(
            family=SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST,
            side=Side.LONG,
            destination=target(Side.LONG, 110.0),
        )
    )
    with pytest.raises(ValueError, match="not registered"):
        adapt_authority_to_flow_scene(authority, tick_size=0.1)
