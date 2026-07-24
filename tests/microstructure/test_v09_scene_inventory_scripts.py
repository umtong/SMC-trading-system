from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import PriceZone, Side
from ictbt.microstructure import FlowSceneKind, FrozenFlowScene
from ictbt.microstructure.scene_adapter import AdaptedFlowScene
from ictbt.microstructure.scene_manifest import record_from_adapted_scene
from scripts.aggregate_easychart_v09_scene_manifest import (
    _load_checkpoint,
    _registered_contract,
)
from scripts.prepare_easychart_v09_scene_month import (
    _preferred_v03,
    _registered_month,
)
from scripts.v09_contract import (
    HOLDOUT_END,
    REGISTERED_MONTHS,
    RESEARCH_START,
    SYMBOL_TICKS,
    WARMUP_DAYS,
    month_bounds,
    monthly_starts,
)


def test_registered_contract_is_exactly_36_months_times_two_symbols() -> None:
    assert len(REGISTERED_MONTHS) == 36
    assert REGISTERED_MONTHS[0] == pd.Timestamp("2021-01-01T00:00:00Z")
    assert REGISTERED_MONTHS[-1] == pd.Timestamp("2023-12-01T00:00:00Z")
    assert len(_registered_contract()) == 72
    assert _registered_contract()[0] == ("BTCUSDT", "2021-01")
    assert _registered_contract()[-1] == ("ETHUSDT", "2023-12")
    assert WARMUP_DAYS == 28


def test_month_contract_is_half_open_and_locked() -> None:
    begin, end = month_bounds("2022-02-15")
    assert begin == pd.Timestamp("2022-02-01T00:00:00Z")
    assert end == pd.Timestamp("2022-03-01T00:00:00Z")
    assert monthly_starts(RESEARCH_START, HOLDOUT_END) == REGISTERED_MONTHS
    assert _registered_month("2021-01") == (
        pd.Timestamp("2021-01-01T00:00:00Z"),
        pd.Timestamp("2021-02-01T00:00:00Z"),
    )
    with pytest.raises(ValueError, match="outside"):
        _registered_month("2024-01")


def authority(
    name: str,
    *,
    confirmation: str,
    overlap: bool,
    width: float,
    known_at: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        authority_id=name,
        confirmation=SimpleNamespace(authority_id=confirmation),
        has_literal_body_overlap=overlap,
        zone=PriceZone(100.0, 100.0 + width),
        known_at=pd.Timestamp(known_at),
    )


def test_v03_duplicate_root_priority_prefers_overlap_then_narrower_zone() -> None:
    candidates = (
        authority(
            "wide-no-overlap",
            confirmation="root-a",
            overlap=False,
            width=2.0,
            known_at="2022-01-01T00:10:00Z",
        ),
        authority(
            "wide-overlap",
            confirmation="root-a",
            overlap=True,
            width=2.0,
            known_at="2022-01-01T00:05:00Z",
        ),
        authority(
            "narrow-overlap",
            confirmation="root-a",
            overlap=True,
            width=1.0,
            known_at="2022-01-01T00:00:00Z",
        ),
        authority(
            "root-b",
            confirmation="root-b",
            overlap=False,
            width=1.0,
            known_at="2022-01-02T00:00:00Z",
        ),
    )
    selected = _preferred_v03(candidates)

    assert [item.authority_id for item in selected] == ["narrow-overlap", "root-b"]


def manifest_record():
    scene = FrozenFlowScene(
        scene_id="scene",
        symbol="BTCUSDT",
        side=Side.LONG,
        kind=FlowSceneKind.BREAK_ACCEPTANCE,
        node_price=100.0,
        known_at=pd.Timestamp("2021-01-15T12:00:00Z"),
        initial_stop=99.0,
        initial_target=102.0,
        tick_size=SYMBOL_TICKS["BTCUSDT"],
    )
    adapted = AdaptedFlowScene(
        scene=scene,
        source_authority_id="scene",
        source_scene_family=scene_family(),
        source_target_id="target",
    )
    return record_from_adapted_scene(adapted)


def scene_family():
    from ictbt.easychart_v0.domain import SceneFamily

    return SceneFamily.SR_FLIP_FVG


def write_checkpoint(path: Path, *, records: int = 1) -> None:
    record = asdict(manifest_record())
    payload = {
        "schema_version": 1,
        "contract": "easychart_v09_outcome_blind_scene_month",
        "symbol": "BTCUSDT",
        "month": "2021-01",
        "evaluation_start": "2021-01-01T00:00:00+00:00",
        "evaluation_end": "2021-02-01T00:00:00+00:00",
        "warmup_start": "2020-12-04T00:00:00+00:00",
        "warmup_days": 28,
        "tick_size": SYMBOL_TICKS["BTCUSDT"],
        "outcome_blind_selection": True,
        "diagnostics": {"manifest_records": records},
        "records": [record],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_checkpoint_loader_validates_identity_tick_and_record_count(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_2021-01.json"
    write_checkpoint(path)
    records, diagnostics, provenance = _load_checkpoint(
        path,
        symbol="BTCUSDT",
        month="2021-01",
    )

    assert len(records) == 1
    assert diagnostics["manifest_records"] == 1
    assert provenance["records"] == 1
    assert len(str(provenance["sha256"])) == 64

    write_checkpoint(path, records=2)
    with pytest.raises(ValueError, match="record count"):
        _load_checkpoint(path, symbol="BTCUSDT", month="2021-01")


def test_checkpoint_loader_rejects_wrong_registered_identity(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path)
    with pytest.raises(ValueError, match="identity mismatch"):
        _load_checkpoint(path, symbol="BTCUSDT", month="2021-02")
    with pytest.raises(ValueError, match="identity mismatch"):
        _load_checkpoint(path, symbol="ETHUSDT", month="2021-01")
