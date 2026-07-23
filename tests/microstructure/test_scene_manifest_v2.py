from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import SceneFamily, Side
from ictbt.microstructure import DualClockSceneKind, FrozenDualClockScene
from ictbt.microstructure.scene_adapter_v1 import AdaptedDualClockScene
from ictbt.microstructure.scene_manifest_v2 import (
    build_dual_clock_scene_manifest,
    load_dual_clock_scene_manifest,
    record_from_dual_clock_scene,
    required_dates_by_symbol_v2,
    write_dual_clock_scene_manifest,
)


def adapted() -> AdaptedDualClockScene:
    scene = FrozenDualClockScene(
        scene_id="scene-one",
        symbol="BTCUSDT",
        side=Side.LONG,
        kind=DualClockSceneKind.SWEEP_REVERSAL,
        node_price=100.0,
        event_started_at=pd.Timestamp("2025-01-02T00:02:00Z"),
        event_known_at=pd.Timestamp("2025-01-02T00:17:00Z"),
        confirmation_started_at=pd.Timestamp("2025-01-02T00:20:00Z"),
        confirmation_known_at=pd.Timestamp("2025-01-02T00:25:00Z"),
        initial_stop=99.0,
        initial_target=102.0,
        tick_size=0.1,
    )
    return AdaptedDualClockScene(
        scene=scene,
        source_authority_id="authority-one",
        source_scene_family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
        source_target_id="target-one",
        source_event_id="event-one",
        source_confirmation_id="confirmation-one",
    )


def test_manifest_preserves_both_clocks_and_cross_midnight_history() -> None:
    record = record_from_dual_clock_scene(adapted())

    assert record.event_started_at == "2025-01-02T00:02:00+00:00"
    assert record.confirmation_known_at == "2025-01-02T00:25:00+00:00"
    assert record.entry_time == record.confirmation_known_at
    assert record.required_utc_dates == ("2025-01-01", "2025-01-02")
    assert record.flow_start < record.event_started_at
    assert record.flow_end > record.entry_time


def test_schema_two_manifest_round_trips_canonically(tmp_path) -> None:
    record = record_from_dual_clock_scene(adapted())
    manifest = build_dual_clock_scene_manifest(
        (record,),
        research_start="2025-01-01",
        research_end="2025-02-01",
        generated_at="2026-07-23T00:00:00Z",
    )
    path = tmp_path / "manifest.json"
    write_dual_clock_scene_manifest(manifest, path)

    loaded = load_dual_clock_scene_manifest(path)

    assert loaded == manifest
    assert loaded.schema_version == 2
    assert loaded.outcome_blind_selection
    assert required_dates_by_symbol_v2(loaded) == {
        "BTCUSDT": ("2025-01-01", "2025-01-02")
    }


def test_manifest_rejects_entry_clock_not_owned_by_confirmation() -> None:
    record = record_from_dual_clock_scene(adapted())
    bad = record.__class__(
        **{
            **record.__dict__,
            "entry_time": "2025-01-02T00:26:00+00:00",
        }
    )
    with pytest.raises(ValueError, match="entry_time"):
        build_dual_clock_scene_manifest(
            (bad,),
            research_start="2025-01-01",
            research_end="2025-02-01",
        )
