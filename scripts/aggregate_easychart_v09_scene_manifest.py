from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
from typing import Iterable

from ictbt.microstructure.scene_manifest import (
    SceneManifestRecord,
    build_scene_manifest,
    required_dates_by_symbol,
    write_scene_manifest,
)
from scripts.v09_contract import (
    HOLDOUT_END,
    REGISTERED_MONTHS,
    RESEARCH_START,
    SYMBOL_TICKS,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify every registered BTC/ETH month checkpoint and produce the "
            "canonical outcome-blind V0.9 scene manifest."
        )
    )
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _checkpoint_path(prepared_dir: Path, symbol: str, month: str) -> Path:
    return prepared_dir / f"{symbol}_{month}.json"


def _load_checkpoint(
    path: Path,
    *,
    symbol: str,
    month: str,
) -> tuple[list[SceneManifestRecord], dict[str, object], dict[str, object]]:
    if not path.is_file():
        raise FileNotFoundError(f"missing registered V0.9 scene checkpoint: {path}")
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported checkpoint schema: {path}")
    if payload.get("contract") != "easychart_v09_outcome_blind_scene_month":
        raise ValueError(f"unexpected checkpoint contract: {path}")
    if payload.get("symbol") != symbol or payload.get("month") != month:
        raise ValueError(f"checkpoint identity mismatch: {path}")
    if payload.get("outcome_blind_selection") is not True:
        raise ValueError(f"checkpoint is not outcome-blind: {path}")
    expected_tick = float(SYMBOL_TICKS[symbol])
    if float(payload.get("tick_size")) != expected_tick:
        raise ValueError(f"checkpoint tick-size mismatch: {path}")

    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError(f"checkpoint records must be a list: {path}")
    records = [
        SceneManifestRecord(
            **{
                **record,
                "required_utc_dates": tuple(record["required_utc_dates"]),
            }
        )
        for record in raw_records
    ]
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise ValueError(f"checkpoint diagnostics must be an object: {path}")
    if int(diagnostics.get("manifest_records", -1)) != len(records):
        raise ValueError(f"checkpoint record count disagrees with diagnostics: {path}")
    provenance = {
        "symbol": symbol,
        "month": month,
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "records": len(records),
    }
    return records, diagnostics, provenance


def _registered_contract() -> tuple[tuple[str, str], ...]:
    return tuple(
        (symbol, month.strftime("%Y-%m"))
        for month in REGISTERED_MONTHS
        for symbol in sorted(SYMBOL_TICKS)
    )


def _write_rows(path: Path, rows: Iterable[dict[str, object]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in materialized for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(materialized)


def main() -> int:
    args = _args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_records: list[SceneManifestRecord] = []
    checkpoint_rows: list[dict[str, object]] = []
    provenance: list[dict[str, object]] = []

    for symbol, month in _registered_contract():
        records, diagnostics, source = _load_checkpoint(
            _checkpoint_path(args.prepared_dir, symbol, month),
            symbol=symbol,
            month=month,
        )
        all_records.extend(records)
        checkpoint_rows.append(
            {
                "symbol": symbol,
                "month": month,
                **diagnostics,
            }
        )
        provenance.append(source)

    expected_checkpoints = len(REGISTERED_MONTHS) * len(SYMBOL_TICKS)
    if len(provenance) != expected_checkpoints:
        raise AssertionError("registered V0.9 checkpoint count changed")
    manifest = build_scene_manifest(
        all_records,
        research_start=RESEARCH_START,
        research_end=HOLDOUT_END,
    )
    manifest_path = args.output_dir / "v09_scene_manifest.json"
    write_scene_manifest(manifest, manifest_path)

    family_counts = Counter(record.source_scene_family for record in manifest.records)
    side_counts = Counter(record.side for record in manifest.records)
    symbol_counts = Counter(record.symbol for record in manifest.records)
    month_counts = Counter(record.known_at[:7] for record in manifest.records)
    dates = required_dates_by_symbol(manifest)
    summary = {
        "schema_version": 1,
        "contract": "easychart_v09_outcome_blind_scene_inventory",
        "research_start": RESEARCH_START,
        "research_end": HOLDOUT_END,
        "registered_months": len(REGISTERED_MONTHS),
        "registered_symbols": sorted(SYMBOL_TICKS),
        "registered_checkpoints": expected_checkpoints,
        "scene_records": len(manifest.records),
        "family_counts": dict(sorted(family_counts.items())),
        "side_counts": dict(sorted(side_counts.items())),
        "symbol_counts": dict(sorted(symbol_counts.items())),
        "months_with_scenes": len(month_counts),
        "monthly_scene_counts": dict(sorted(month_counts.items())),
        "required_dates_by_symbol": dates,
        "required_archive_days": sum(len(items) for items in dates.values()),
        "scene_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "checkpoint_provenance": provenance,
    }
    (args.output_dir / "v09_scene_inventory_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_rows(args.output_dir / "v09_month_diagnostics.csv", checkpoint_rows)
    _write_rows(
        args.output_dir / "v09_scene_inventory.csv",
        [
            {
                **record.__dict__,
                "required_utc_dates": "|".join(record.required_utc_dates),
            }
            for record in manifest.records
        ],
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "checkpoint_provenance"}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
