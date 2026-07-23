from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from .scene_adapter import AdaptedFlowScene


FLOW_HISTORY_MINUTES = 125
FLOW_FORWARD_MINUTES = 1


@dataclass(frozen=True, slots=True)
class SceneManifestRecord:
    scene_id: str
    source_authority_id: str
    source_scene_family: str
    source_target_id: str
    symbol: str
    side: str
    kind: str
    node_price: float
    known_at: str
    initial_stop: float
    initial_target: float
    tick_size: float
    flow_start: str
    flow_end: str
    required_utc_dates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SceneManifest:
    schema_version: int
    generated_at_utc: str
    research_start: str
    research_end: str
    outcome_blind_selection: bool
    records: tuple[SceneManifestRecord, ...]

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False, indent=2)


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be valid")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def required_flow_interval(
    known_at: object,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    known = _utc(known_at, name="known_at")
    return (
        known - pd.Timedelta(minutes=FLOW_HISTORY_MINUTES),
        known + pd.Timedelta(minutes=FLOW_FORWARD_MINUTES),
    )


def required_utc_dates(known_at: object) -> tuple[str, ...]:
    start, end = required_flow_interval(known_at)
    final = (end - pd.Timedelta(nanoseconds=1)).normalize()
    return tuple(
        timestamp.strftime("%Y-%m-%d")
        for timestamp in pd.date_range(start.normalize(), final, freq="1D", tz="UTC")
    )


def record_from_adapted_scene(adapted: AdaptedFlowScene) -> SceneManifestRecord:
    scene = adapted.scene
    start, end = required_flow_interval(scene.known_at)
    return SceneManifestRecord(
        scene_id=scene.scene_id,
        source_authority_id=adapted.source_authority_id,
        source_scene_family=adapted.source_scene_family.value,
        source_target_id=adapted.source_target_id,
        symbol=scene.symbol,
        side=scene.side.value,
        kind=scene.kind.value,
        node_price=scene.node_price,
        known_at=scene.known_at.isoformat(),
        initial_stop=scene.initial_stop,
        initial_target=scene.initial_target,
        tick_size=scene.tick_size,
        flow_start=start.isoformat(),
        flow_end=end.isoformat(),
        required_utc_dates=required_utc_dates(scene.known_at),
    )


def _record_key(record: SceneManifestRecord) -> tuple[str, str]:
    return record.source_scene_family, record.scene_id


def build_scene_manifest(
    records: Iterable[SceneManifestRecord],
    *,
    research_start: object,
    research_end: object,
    generated_at: object | None = None,
) -> SceneManifest:
    start = _utc(research_start, name="research_start")
    end = _utc(research_end, name="research_end")
    if end <= start:
        raise ValueError("research_end must follow research_start")
    generated = _utc(
        pd.Timestamp.now(tz="UTC") if generated_at is None else generated_at,
        name="generated_at",
    )
    by_key: dict[tuple[str, str], SceneManifestRecord] = {}
    for record in records:
        known = _utc(record.known_at, name="record known_at")
        if not start <= known < end:
            raise ValueError(
                f"scene {record.scene_id} is outside the registered research interval"
            )
        key = _record_key(record)
        previous = by_key.get(key)
        if previous is not None and previous != record:
            raise ValueError(f"conflicting duplicate scene manifest record: {key}")
        by_key[key] = record
    ordered = tuple(
        sorted(
            by_key.values(),
            key=lambda item: (item.known_at, item.symbol, item.source_scene_family, item.scene_id),
        )
    )
    return SceneManifest(
        schema_version=1,
        generated_at_utc=generated.isoformat(),
        research_start=start.isoformat(),
        research_end=end.isoformat(),
        outcome_blind_selection=True,
        records=ordered,
    )


def write_scene_manifest(manifest: SceneManifest, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest.to_json(), encoding="utf-8")


def load_scene_manifest(path: str | Path) -> SceneManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("unsupported scene manifest schema")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("scene manifest records must be a list")
    records = tuple(
        SceneManifestRecord(
            **{
                **item,
                "required_utc_dates": tuple(item["required_utc_dates"]),
            }
        )
        for item in raw_records
    )
    manifest = SceneManifest(
        schema_version=1,
        generated_at_utc=str(payload["generated_at_utc"]),
        research_start=str(payload["research_start"]),
        research_end=str(payload["research_end"]),
        outcome_blind_selection=bool(payload["outcome_blind_selection"]),
        records=records,
    )
    if not manifest.outcome_blind_selection:
        raise ValueError("scene manifest must declare outcome-blind selection")
    # Rebuild to validate clocks, range and duplicate identity without changing
    # the persisted generation timestamp.
    rebuilt = build_scene_manifest(
        manifest.records,
        research_start=manifest.research_start,
        research_end=manifest.research_end,
        generated_at=manifest.generated_at_utc,
    )
    if rebuilt != manifest:
        raise ValueError("scene manifest is not canonical")
    return manifest


def required_dates_by_symbol(
    manifest: SceneManifest,
) -> dict[str, tuple[str, ...]]:
    dates: dict[str, set[str]] = {}
    for record in manifest.records:
        dates.setdefault(record.symbol, set()).update(record.required_utc_dates)
    return {
        symbol: tuple(sorted(values))
        for symbol, values in sorted(dates.items())
    }


__all__ = [
    "FLOW_FORWARD_MINUTES",
    "FLOW_HISTORY_MINUTES",
    "SceneManifest",
    "SceneManifestRecord",
    "build_scene_manifest",
    "load_scene_manifest",
    "record_from_adapted_scene",
    "required_dates_by_symbol",
    "required_flow_interval",
    "required_utc_dates",
    "write_scene_manifest",
]
