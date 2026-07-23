#!/usr/bin/env python3
"""Compatibility collector preserving unavailable Binance metrics as missing.

Empty source fields are never filled with zero or future values. They remain empty
in the canonical CSV and are counted in the manifest so downstream point-in-time
research can exclude only decisions whose required feature was unavailable.
"""

from __future__ import annotations

import csv
import gzip
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

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
        absolute = abs(value)
        if absolute < 10**11:
            value *= 1000
        elif absolute >= 10**15:
            value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible metrics timestamp: {raw}")
    if value % base.INTERVAL_MS != 0:
        raise ValueError(f"metrics timestamp is not on a 5m UTC grid: {raw}")
    return value


def rows_from_zip(path: Path, expected_symbol: str) -> Iterator[list[str]]:
    with base.zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}: {names}")
        with archive.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            try:
                header = base.normalize_header(next(reader))
            except StopIteration as exc:
                raise ValueError(f"empty metrics archive: {path}") from exc
            if header != base.SOURCE_COLUMNS:
                raise ValueError(f"unexpected metrics header in {path}: {header}")
            for line_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) != len(base.SOURCE_COLUMNS):
                    raise ValueError(f"wrong field count {path}:{line_number}: {len(row)}")
                symbol = row[1].strip().upper()
                if symbol != expected_symbol:
                    raise ValueError(f"symbol mismatch {path}:{line_number}: {symbol}")
                timestamp = epoch_ms(row[0])
                normalized_metrics: list[str] = []
                for column, raw_value in zip(base.SOURCE_COLUMNS[2:], row[2:], strict=True):
                    text = raw_value.strip()
                    if text == "":
                        normalized_metrics.append("")
                        continue
                    value = float(text)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(
                            f"invalid {column} value {path}:{line_number}: {raw_value!r}"
                        )
                    normalized_metrics.append(format(value, ".17g"))
                yield [str(timestamp), symbol, *normalized_metrics]


def build_symbol(
    symbol: str,
    downloaded: Sequence[base.DownloadedDay],
    output_dir: Path,
) -> tuple[list[base.SourceRecord], dict[str, object]]:
    output_path = output_dir / f"{symbol}_metrics_5m.csv.gz"
    sources: list[base.SourceRecord] = []
    previous: int | None = None
    first: int | None = None
    last: int | None = None
    total_rows = 0
    gap_transitions = 0
    missing_intervals = 0
    duplicates = 0
    zero_counts = {column: 0 for column in base.CANONICAL_COLUMNS[2:]}
    missing_counts = {column: 0 for column in base.CANONICAL_COLUMNS[2:]}
    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(base.CANONICAL_COLUMNS)
        for item in sorted(downloaded, key=lambda value: value.day):
            count = 0
            day_first: int | None = None
            day_last: int | None = None
            for row in rows_from_zip(item.zip_path, symbol):
                timestamp = int(row[0])
                if previous is not None:
                    delta = timestamp - previous
                    if delta == 0:
                        duplicates += 1
                    if delta <= 0:
                        raise ValueError(f"non-increasing metrics time {symbol}: {timestamp}")
                    if delta != base.INTERVAL_MS:
                        gap_transitions += 1
                        if delta > base.INTERVAL_MS and delta % base.INTERVAL_MS == 0:
                            missing_intervals += delta // base.INTERVAL_MS - 1
                previous = timestamp
                first = timestamp if first is None else first
                last = timestamp
                day_first = timestamp if day_first is None else day_first
                day_last = timestamp
                for column, raw_value in zip(base.CANONICAL_COLUMNS[2:], row[2:], strict=True):
                    if raw_value == "":
                        missing_counts[column] += 1
                    else:
                        zero_counts[column] += int(float(raw_value) == 0.0)
                writer.writerow(row)
                total_rows += 1
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
        "rows": total_rows,
        "first_create_time_ms": first,
        "last_create_time_ms": last,
        "gap_transitions": gap_transitions,
        "missing_5m_intervals": missing_intervals,
        "duplicate_create_times": duplicates,
        "zero_counts": zero_counts,
        "missing_counts": missing_counts,
        "missing_value_policy": "source empty fields preserved as empty; no imputation",
        "output": output_path.name,
        "output_sha256": base.sha256(output_path),
        "output_bytes": output_path.stat().st_size,
    }


base.epoch_ms = epoch_ms
base.rows_from_zip = rows_from_zip
base.build_symbol = build_symbol

if __name__ == "__main__":
    raise SystemExit(base.main())
