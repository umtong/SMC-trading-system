from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from .dual_clock import required_dual_clock_flow_interval
from .scene_adapter_v1 import AdaptedDualClockScene


@dataclass(frozen=True, slots=True)
class DualClockSceneManifestRecord:
    scene_id: str
    source_authority_id: str
    source_scene_family: str
    source_target_id: str
    source_event_id: str
    source_confirmation_id: str
    symbol: str
    side: str
    kind: str
    node_price: float
    event_started_at: str
    event_known_at: str
    confirmation_started_at: str
    confirmation_known_at: str
    entry_time: str
    initial_stop: float
    initial_target: float
    tick_size: float
    flow_start: str
    flow_end: str
    required_utc_dates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DualClockSceneManifest:
    schema_version: int
    generated_at_utc: str
    research_start: str
    research_end: str
    outcome_blind_selection: bool
    records: tuple[DualClockSceneManifestRecord, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be valid")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def required_utc_dates_v2(
    adapted: AdaptedDualClockScene,
) -> tuple[str, ...]:
    start, end = required_dual_clock_flow_interval(adapted.scene)
    final = (end - pd.Timedelta(nanoseconds=1)).normalize()
    return tuple(
        timestamp.strftime("%Y-%m-%d")
        for timestamp in pd.date_range(start.normalize(), final, freq="1D", tz="UTC")
    )


def record_from_dual_clock_scene(
    adapted: AdaptedDualClockScene,
) -> DualClockSceneManifestRecord:
    scene = adapted.scene
    start, end = required_dual_clock_flow_interval(scene)
    return DualClockSceneManifestRecord(
        scene_id=scene.scene_id,
        source_authority_id=adapted.source_authority_id,
        source_scene_family=adapted.source_scene_family.value,
        source_target_id=adapted.source_target_id,
        source_event_id=adapted.source_event_id,
        source_confirmation_id=adapted.source_confirmation_id,
        symbol=scene.symbol,
        side=scene.side.value,
        kind=scene.kind.value,
        node_price=scene.node_price,
        event_started_at=scene.event_started_at.isoformat(),
        event_known_at=scene.event_known_at.isoformat(),
        confirmation_started_at=scene.confirmation_started_at.isoformat(),
        confirmation_known_at=scene.confirmation_known_at.isoformat(),
        entry_time=scene.entry_time.isoformat(),
        initial_stop=scene.initial_stop,
        initial_target=scene.initial_target,
        tick_size=scene.tick_size,
        flow_start=start.isoformat(),
        flow_end=end.isoformat(),
        required_utc_dates=required_utc_dates_v2(adapted),
    )


def _record_key(record: DualClockSceneManifestRecord) -> tuple[str, str]:
    return record.source_scene_family, record.scene_id


def build_dual_clock_scene_manifest(
    records: Iterable[DualClockSceneManifestRecord],
    *,
    research_start: object,
    research_end: object,
    generated_at: object | None = None,
) -> DualClockSceneManifest:
    start = _utc(research_start, name="research_start")
    end = _utc(research_end, name="research_end")
    if end <= start:
        raise ValueError("research_end must follow research_start")
    generated = _utc(
        pd.Timestamp.now(tz="UTC") if generated_at is None else generated_at,
        name="generated_at",
    )
    by_key: dict[tuple[str, str], DualClockSceneManifestRecord] = {}
    for record in records:
        confirmation_known = _utc(
            record.confirmation_known_at,
            name="record confirmation_known_at",
        )
        if not start <= confirmation_known < end:
            raise ValueError(
                f"scene {record.scene_id} is outside the registered research interval"
            )
        if record.entry_time != record.confirmation_known_at:
            raise ValueError("entry_time must equal the completed confirmation clock")
        key = _record_key(record)
        previous = by_key.get(key)
        if previous is not None and previous != record:
            raise ValueError(f"conflicting duplicate dual-clock scene: {key}")
        by_key[key] = record
    ordered = tuple(
        sorted(
            by_key.values(),
            key=lambda item: (
                item.confirmation_known_at,
                item.symbol,
                item.source_scene_family,
                item.scene_id,
            ),
        )
    )
    return DualClockSceneManifest(
        schema_version=2,
        generated_at_utc=generated.isoformat(),
        research_start=start.isoformat(),
        research_end=end.isoformat(),
        outcome_blind_selection=True,
        records=ordered,
    )


def write_dual_clock_scene_manifest(
    manifest: DualClockSceneManifest,
    path: str | Path,
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest.to_json(), encoding="utf-8")


def load_dual_clock_scene_manifest(
    path: str | Path,
) -> DualClockSceneManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 2:
        raise ValueError("unsupported dual-clock scene manifest schema")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("dual-clock scene manifest records must be a list")
    records = tuple(
        DualClockSceneManifestRecord(
            **{
                **item,
                "required_utc_dates": tuple(item["required_utc_dates"]),
            }
        )
        for item in raw_records
    )
    manifest = DualClockSceneManifest(
        schema_version=2,
        generated_at_utc=str(payload["generated_at_utc"]),
        research_start=str(payload["research_start"]),
        research_end=str(payload["research_end"]),
        outcome_blind_selection=bool(payload["outcome_blind_selection"]),
        records=records,
    )
    if not manifest.outcome_blind_selection:
        raise ValueError("scene manifest must declare outcome-blind selection")
    rebuilt = build_dual_clock_scene_manifest(
        manifest.records,
        research_start=manifest.research_start,
        research_end=manifest.research_end,
        generated_at=manifest.generated_at_utc,
    )
    if rebuilt != manifest:
        raise ValueError("dual-clock scene manifest is not canonical")
    return manifest


def required_dates_by_symbol_v2(
    manifest: DualClockSceneManifest,
) -> dict[str, tuple[str, ...]]:
    dates: dict[str, set[str]] = {}
    for record in manifest.records:
        dates.setdefault(record.symbol, set()).update(record.required_utc_dates)
    return {
        symbol: tuple(sorted(values))
        for symbol, values in sorted(dates.items())
    }


__all__ = [
    "DualClockSceneManifest",
    "DualClockSceneManifestRecord",
    "build_dual_clock_scene_manifest",
    "load_dual_clock_scene_manifest",
    "record_from_dual_clock_scene",
    "required_dates_by_symbol_v2",
    "required_utc_dates_v2",
    "write_dual_clock_scene_manifest",
]
