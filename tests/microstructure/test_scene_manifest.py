from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import SceneFamily, Side
from ictbt.microstructure import FlowSceneKind, FrozenFlowScene
from ictbt.microstructure.scene_adapter import AdaptedFlowScene
from ictbt.microstructure.scene_manifest import (
    build_scene_manifest,
    load_scene_manifest,
    record_from_adapted_scene,
    required_dates_by_symbol,
    required_flow_interval,
    required_utc_dates,
    write_scene_manifest,
)


def adapted(scene_id: str, symbol: str, known_at: str) -> AdaptedFlowScene:
    scene = FrozenFlowScene(
        scene_id=scene_id,
        symbol=symbol,
        side=Side.LONG,
        kind=FlowSceneKind.SWEEP_RECLAIM,
        node_price=100.0,
        known_at=pd.Timestamp(known_at),
        initial_stop=99.0,
        initial_target=102.0,
        tick_size=0.1,
    )
    return AdaptedFlowScene(
        scene=scene,
        source_authority_id=scene_id,
        source_scene_family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
        source_target_id=f"target-{scene_id}",
    )


def test_required_flow_dates_cross_midnight_without_downloading_outcomes() -> None:
    known = pd.Timestamp("2023-01-02T00:30:00Z")
    start, end = required_flow_interval(known)

    assert start == pd.Timestamp("2023-01-01T22:25:00Z")
    assert end == pd.Timestamp("2023-01-02T00:31:00Z")
    assert required_utc_dates(known) == ("2023-01-01", "2023-01-02")


def test_manifest_deduplicates_identical_scene_identity_and_sorts() -> None:
    later = record_from_adapted_scene(
        adapted("later", "ETHUSDT", "2023-01-02T12:00:00Z")
    )
    earlier = record_from_adapted_scene(
        adapted("earlier", "BTCUSDT", "2023-01-01T12:00:00Z")
    )
    manifest = build_scene_manifest(
        (later, earlier, earlier),
        research_start="2023-01-01",
        research_end="2023-02-01",
        generated_at="2023-02-02T00:00:00Z",
    )

    assert manifest.outcome_blind_selection
    assert [item.scene_id for item in manifest.records] == ["earlier", "later"]
    assert required_dates_by_symbol(manifest) == {
        "BTCUSDT": ("2023-01-01",),
        "ETHUSDT": ("2023-01-02",),
    }


def test_conflicting_duplicate_scene_is_hard_error() -> None:
    first = record_from_adapted_scene(
        adapted("same", "BTCUSDT", "2023-01-01T12:00:00Z")
    )
    conflict = replace(first, initial_target=103.0)

    with pytest.raises(ValueError, match="conflicting duplicate"):
        build_scene_manifest(
            (first, conflict),
            research_start="2023-01-01",
            research_end="2023-02-01",
        )


def test_manifest_round_trip_validates_outcome_blind_contract(tmp_path: Path) -> None:
    record = record_from_adapted_scene(
        adapted("scene", "BTCUSDT", "2023-01-01T12:00:00Z")
    )
    manifest = build_scene_manifest(
        (record,),
        research_start="2023-01-01",
        research_end="2023-02-01",
        generated_at="2023-02-02T00:00:00Z",
    )
    path = tmp_path / "manifest.json"
    write_scene_manifest(manifest, path)

    assert load_scene_manifest(path) == manifest

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["outcome_blind_selection"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="outcome-blind"):
        load_scene_manifest(path)


def test_scene_outside_registered_interval_is_rejected() -> None:
    record = record_from_adapted_scene(
        adapted("late", "BTCUSDT", "2023-02-01T00:00:00Z")
    )
    with pytest.raises(ValueError, match="outside"):
        build_scene_manifest(
            (record,),
            research_start="2023-01-01",
            research_end="2023-02-01",
        )
