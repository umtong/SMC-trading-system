#!/usr/bin/env python3
"""Compatibility collector preserving exact Binance metrics availability times.

Some checksum-verified archives publish a nominal five-minute observation one
second after the wall-clock boundary. The exact source timestamp is the causal
`known_at`; it is never rounded backward. A nearest five-minute nominal slot is
used only for continuity diagnostics. Downstream bar decisions must use an as-of
join on `create_time_ms <= decision_time_ms`.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import binance_metrics_bundle as base


def epoch_ms(raw: str) -> int:
    text = str(raw).strip()
    try:
        value = int(float(text))
    except ValueError:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
        parsed = (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
        value = int(round(parsed.timestamp() * 1000.0))
    else:
        magnitude = abs(value)
        if magnitude < 10**11:
            value *= 1000
        elif magnitude >= 10**15:
            value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible metrics timestamp: {raw}")
    return value


def build_symbol(
    symbol: str,
    downloaded: Sequence[base.DownloadedDay],
    output_dir: Path,
) -> tuple[list[base.SourceRecord], dict[str, object]]:
    output_path = output_dir / f"{symbol}_metrics_5m.csv.gz"
    sources: list[base.SourceRecord] = []
    previous_source: int | None = None
    previous_nominal: int | None = None
    first: int | None = None
    last: int | None = None
    rows_total = 0
    nominal_gap_transitions = 0
    missing_nominal_intervals = 0
    duplicate_source_times = 0
    duplicate_nominal_slots = 0
    off_grid_rows = 0
    max_abs_nominal_offset_ms = 0
    zero_counts = {column: 0 for column in base.CANONICAL_COLUMNS[2:]}
    missing_counts = {column: 0 for column in base.CANONICAL_COLUMNS[2:]}

    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(base.CANONICAL_COLUMNS)
        for item in sorted(downloaded, key=lambda value: value.day):
            count = 0
            day_first: int | None = None
            day_last: int | None = None
            for row in base.rows_from_zip(item.zip_path, symbol):
                source_time = int(row[0])
                nominal_time = int(
                    math.floor(source_time / base.INTERVAL_MS + 0.5)
                    * base.INTERVAL_MS
                )
                offset = source_time - nominal_time
                max_abs_nominal_offset_ms = max(
                    max_abs_nominal_offset_ms, abs(offset)
                )
                off_grid_rows += int(offset != 0)
                # More than one minute from the nearest slot is not silently
                # reinterpreted; preserve diagnostics and stop for inspection.
                if abs(offset) > 60_000:
                    raise ValueError(
                        f"metrics source clock exceeds 60s nominal tolerance: "
                        f"{symbol} {source_time} offset={offset}"
                    )
                if previous_source is not None:
                    if source_time == previous_source:
                        duplicate_source_times += 1
                    if source_time <= previous_source:
                        raise ValueError(
                            f"non-increasing exact metrics time {symbol}: {source_time}"
                        )
                if previous_nominal is not None:
                    delta = nominal_time - previous_nominal
                    if delta == 0:
                        duplicate_nominal_slots += 1
                    elif delta < 0:
                        raise ValueError(
                            f"decreasing nominal metrics slot {symbol}: {nominal_time}"
                        )
                    elif delta != base.INTERVAL_MS:
                        nominal_gap_transitions += 1
                        if delta > base.INTERVAL_MS and delta % base.INTERVAL_MS == 0:
                            missing_nominal_intervals += delta // base.INTERVAL_MS - 1
                previous_source = source_time
                previous_nominal = nominal_time
                first = source_time if first is None else first
                last = source_time
                day_first = source_time if day_first is None else day_first
                day_last = source_time
                for column, value in zip(
                    base.CANONICAL_COLUMNS[2:], row[2:], strict=True
                ):
                    if value == "":
                        missing_counts[column] += 1
                    else:
                        zero_counts[column] += int(float(value) == 0.0)
                writer.writerow(row)
                rows_total += 1
                count += 1
            sources.append(
                base.SourceRecord(
                    symbol=symbol,
                    day=item.day,
                    archive_url=item.archive_url,
                    published_sha256=item.published_sha256,
                    observed_sha256=item.observed_sha256,
                    rows=count,
                    first_create_time_ms=day_first,
                    last_create_time_ms=day_last,
                )
            )
            item.zip_path.unlink(missing_ok=True)

    return sources, {
        "rows": rows_total,
        "first_create_time_ms": first,
        "last_create_time_ms": last,
        "exact_known_at_policy": "source create_time preserved; no backward rounding",
        "downstream_join_policy": "latest create_time_ms <= decision_time_ms",
        "nominal_gap_transitions": nominal_gap_transitions,
        "missing_nominal_5m_intervals": missing_nominal_intervals,
        "duplicate_source_times": duplicate_source_times,
        "duplicate_nominal_slots": duplicate_nominal_slots,
        "off_grid_rows": off_grid_rows,
        "max_abs_nominal_offset_ms": max_abs_nominal_offset_ms,
        "zero_counts": zero_counts,
        "missing_counts": missing_counts,
        "missing_value_policy": "source empty fields preserved; no imputation",
        "output": output_path.name,
        "output_sha256": base.sha256(output_path),
        "output_bytes": output_path.stat().st_size,
    }


def output_directory(argv: list[str]) -> Path:
    try:
        index = argv.index("--output-dir")
    except ValueError as exc:
        raise ValueError("--output-dir is required") from exc
    return Path(argv[index + 1])


base.epoch_ms = epoch_ms
base.build_symbol = build_symbol

if __name__ == "__main__":
    directory = output_directory(sys.argv)
    code = base.main()
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["contract"]["timestamp_policy"] = (
            "exact source create_time is causal known_at; nearest 5m slot is audit-only"
        )
        manifest["contract"]["downstream_join_policy"] = (
            "as-of latest create_time_ms <= decision_time_ms"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    raise SystemExit(code)
