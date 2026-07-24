#!/usr/bin/env python3
"""Collect public Deribit BTC/ETH DVOL hourly candles for causal research.

The utility uses only the public JSON-RPC endpoint, snapshots every raw response hash,
preserves returned timestamps, and has no credential or order code.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

ENDPOINT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
RESOLUTION = "3600"
RESOLUTION_MS = 3_600_000


@dataclass(frozen=True)
class RequestRecord:
    currency: str
    start_timestamp: int
    end_timestamp: int
    continuation: int | None
    rows: int
    response_sha256: str
    response_bytes: int


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def month_chunks(start_ms: int, end_ms: int) -> Iterator[tuple[int, int]]:
    cursor = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    finish = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    while cursor < finish:
        if cursor.month == 12:
            next_month = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        else:
            next_month = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        chunk_end = min(next_month, finish)
        yield int(cursor.timestamp() * 1000), int(chunk_end.timestamp() * 1000)
        cursor = chunk_end


def request_json(params: dict[str, object], attempts: int = 7) -> tuple[dict[str, object], bytes]:
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "smc-ict-dvol-research/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
            payload = json.loads(raw)
            if "error" in payload:
                raise RuntimeError(f"Deribit API error: {payload['error']}")
            return payload, raw
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(45.0, 2.0**attempt))
    raise RuntimeError(f"request failed after retries: {url}: {last}")


def validate_row(row: object, currency: str) -> tuple[int, float, float, float, float]:
    if not isinstance(row, list) or len(row) != 5:
        raise ValueError(f"unexpected {currency} DVOL row: {row!r}")
    timestamp = int(row[0])
    values = tuple(float(value) for value in row[1:])
    if timestamp < 10**12 or timestamp >= 10**14:
        raise ValueError(f"implausible DVOL timestamp: {timestamp}")
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError(f"invalid DVOL OHLC: {row!r}")
    opened, high, low, close = values
    if high + 1e-12 < max(opened, close) or low - 1e-12 > min(opened, close) or high < low:
        raise ValueError(f"invalid DVOL geometry: {row!r}")
    return timestamp, opened, high, low, close


def collect_currency(currency: str, start_ms: int, end_ms: int, output_dir: Path) -> tuple[list[RequestRecord], dict[str, object]]:
    rows_by_timestamp: dict[int, tuple[int, float, float, float, float]] = {}
    records: list[RequestRecord] = []
    for chunk_start, chunk_end in month_chunks(start_ms, end_ms):
        request_end = chunk_end
        seen_continuations: set[int] = set()
        while request_end > chunk_start:
            params = {
                "currency": currency,
                "start_timestamp": chunk_start,
                "end_timestamp": request_end,
                "resolution": RESOLUTION,
            }
            payload, raw = request_json(params)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"missing result object for {currency}: {payload!r}")
            data = result.get("data")
            if not isinstance(data, list):
                raise ValueError(f"missing data array for {currency}: {payload!r}")
            parsed_rows = [validate_row(row, currency) for row in data]
            for row in parsed_rows:
                existing = rows_by_timestamp.get(row[0])
                if existing is not None and existing != row:
                    raise ValueError(f"conflicting duplicate DVOL candle {currency} {row[0]}")
                rows_by_timestamp[row[0]] = row
            continuation_raw = result.get("continuation")
            continuation = None if continuation_raw is None else int(continuation_raw)
            records.append(
                RequestRecord(
                    currency=currency,
                    start_timestamp=chunk_start,
                    end_timestamp=request_end,
                    continuation=continuation,
                    rows=len(parsed_rows),
                    response_sha256=sha256_bytes(raw),
                    response_bytes=len(raw),
                )
            )
            print(
                f"[{currency}] {datetime.fromtimestamp(chunk_start/1000, tz=UTC):%Y-%m} "
                f"rows={len(parsed_rows)} continuation={continuation}",
                flush=True,
            )
            if continuation is None:
                break
            if continuation in seen_continuations or not chunk_start < continuation < request_end:
                raise ValueError(f"invalid/repeated continuation for {currency}: {continuation}")
            seen_continuations.add(continuation)
            request_end = continuation

    ordered = [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]
    output_path = output_dir / f"{currency}_DVOL_1h.csv.gz"
    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(("open_time_ms", "open", "high", "low", "close"))
        writer.writerows(ordered)
    timestamps = [row[0] for row in ordered]
    deltas = [right - left for left, right in zip(timestamps, timestamps[1:])]
    nonhourly = sum(delta != RESOLUTION_MS for delta in deltas)
    missing_hours = sum(max(0, delta // RESOLUTION_MS - 1) for delta in deltas if delta > RESOLUTION_MS and delta % RESOLUTION_MS == 0)
    metadata = {
        "rows": len(ordered),
        "first_open_time_ms": timestamps[0] if timestamps else None,
        "last_open_time_ms": timestamps[-1] if timestamps else None,
        "nonhourly_transitions": nonhourly,
        "missing_hours_on_hourly_grid": missing_hours,
        "output": output_path.name,
        "output_sha256": sha256_file(output_path),
        "output_bytes": output_path.stat().st_size,
    }
    return records, metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--currencies", nargs="+", default=("BTC", "ETH"))
    parser.add_argument("--start", default="2021-01-01T00:00:00Z")
    parser.add_argument("--end", default="2026-04-01T00:00:00Z")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    start_ms = timestamp_ms(args.start)
    end_ms = timestamp_ms(args.end)
    if start_ms >= end_ms:
        raise ValueError("start must precede end")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    currencies = tuple(str(value).upper() for value in args.currencies)
    all_records: list[RequestRecord] = []
    datasets: dict[str, object] = {}
    for currency in currencies:
        records, metadata = collect_currency(currency, start_ms, end_ms, args.output_dir)
        all_records.extend(records)
        datasets[currency] = metadata
    manifest = {
        "contract": {
            "status": "RESEARCH_ONLY_PUBLIC_DERIBIT_DVOL_SNAPSHOT",
            "endpoint": ENDPOINT,
            "currencies": list(currencies),
            "start": args.start,
            "end_exclusive": args.end,
            "resolution_seconds": int(RESOLUTION),
            "timestamp_interpretation": "returned candle timestamp preserved as open_time; available only after open_time+1h",
            "raw_response_hashes_preserved": True,
            "credentials_used": False,
            "orders_submitted": False,
        },
        "datasets": datasets,
        "requests": [asdict(record) for record in all_records],
        "retrieved_at": datetime.now(tz=UTC).isoformat(),
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest["datasets"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
