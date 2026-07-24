#!/usr/bin/env python3
"""Extract only preregistered liquidation-event windows from official Binance USD-M aggTrades.

Research-only public-data utility. It verifies Binance Vision's published SHA-256,
preserves the native aggregate-trade fields, and never accesses credentials or order endpoints.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import csv
import gzip
import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator, Sequence

BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time_ms",
    "is_buyer_maker",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "smc-ict-liquidation-tick-research/1.0"}
    )
    with urllib.request.urlopen(request, timeout=240) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def published_checksum(path: Path, filename: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    candidates = [
        token.lower()
        for token in text.replace("*", " ").split()
        if len(token) == 64 and all(char in "0123456789abcdefABCDEF" for char in token)
    ]
    if filename not in text or not candidates:
        raise ValueError(f"invalid checksum file for {filename}")
    return candidates[0]


def epoch_ms(raw: str) -> int:
    value = int(float(raw))
    if abs(value) >= 10**15:
        value //= 1000
    if not 10**12 <= value <= 10**14:
        raise ValueError(f"implausible aggregate-trade timestamp: {raw}")
    return value


def decode_windows(path: Path, symbol: str, month: str) -> list[tuple[int, int]]:
    encoded = "".join(path.read_text(encoding="utf-8").split())
    raw = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
    from datetime import datetime, timezone

    start = int(datetime.fromisoformat(f"{month}-01T00:00:00+00:00").timestamp() * 1000)
    year, mon = map(int, month.split("-"))
    end_dt = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if mon == 12
        else datetime(year, mon + 1, 1, tzinfo=timezone.utc)
    )
    end = int(end_dt.timestamp() * 1000)
    rows: list[tuple[int, int]] = []
    for row in csv.DictReader(io.StringIO(raw)):
        if row["symbol"] != symbol:
            continue
        left = int(row["start_ms"])
        right = int(row["end_ms"])
        if left < end and right > start:
            rows.append((left, right))
    rows.sort()
    for previous, current in zip(rows, rows[1:]):
        if current[0] < previous[1]:
            raise ValueError("event windows must be pre-merged and non-overlapping")
    if not rows:
        raise ValueError(f"no extraction windows for {symbol} {month}")
    return rows


def iter_rows(path: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}: {names}")
        with archive.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            for row in reader:
                if row:
                    yield [item.strip() for item in row]


def normalize(row: Sequence[str], *, filename: str, line_number: int) -> list[str] | None:
    if row[0].lower().replace(" ", "_") in {"agg_trade_id", "aggtradeid"}:
        return None
    if len(row) < 7:
        raise ValueError(f"short aggTrade row {filename}:{line_number}")
    output = list(row[:7])
    for index in (0, 3, 4):
        output[index] = str(int(float(output[index])))
    price = float(output[1])
    quantity = float(output[2])
    if price <= 0 or quantity <= 0:
        raise ValueError(f"non-positive price/quantity {filename}:{line_number}")
    output[5] = str(epoch_ms(output[5]))
    maker = output[6].strip().lower()
    if maker not in {"true", "false"}:
        raise ValueError(f"invalid buyer-maker flag {filename}:{line_number}: {output[6]}")
    output[6] = maker
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--windows-b64", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    symbol = args.symbol.upper()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    windows = decode_windows(args.windows_b64, symbol, args.month)
    starts = [item[0] for item in windows]

    filename = f"{symbol}-aggTrades-{args.month}.zip"
    url = f"{BASE}/{symbol}/{filename}"
    archive_path = args.output_dir / filename
    checksum_path = args.output_dir / f"{filename}.CHECKSUM"
    download(url + ".CHECKSUM", checksum_path)
    download(url, archive_path)
    published = published_checksum(checksum_path, filename)
    observed = sha256(archive_path)
    if observed != published:
        raise ValueError(f"checksum mismatch: {observed} != {published}")

    output_path = args.output_dir / f"{symbol}_aggTrades_{args.month}_event_windows.csv.gz"
    rows_total = 0
    rows_selected = 0
    first_time: int | None = None
    last_time: int | None = None
    prior_id: int | None = None
    duplicate_ids = 0
    nonmonotonic_ids = 0
    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for line_number, raw in enumerate(iter_rows(archive_path), start=1):
            row = normalize(raw, filename=filename, line_number=line_number)
            if row is None:
                continue
            rows_total += 1
            trade_id = int(row[0])
            if prior_id is not None:
                duplicate_ids += int(trade_id == prior_id)
                nonmonotonic_ids += int(trade_id < prior_id)
            prior_id = trade_id
            timestamp = int(row[5])
            pos = bisect.bisect_right(starts, timestamp) - 1
            if pos < 0 or not (windows[pos][0] <= timestamp < windows[pos][1]):
                continue
            writer.writerow(row)
            rows_selected += 1
            first_time = timestamp if first_time is None else min(first_time, timestamp)
            last_time = timestamp if last_time is None else max(last_time, timestamp)

    manifest = {
        "contract": {
            "status": "RESEARCH_ONLY_EVENT_WINDOW_EXTRACTION",
            "source": "Binance Vision USD-M monthly aggTrades",
            "symbol": symbol,
            "month": args.month,
            "event_windows": len(windows),
            "half_open_window_rule": "start_ms <= transact_time_ms < end_ms",
            "credentials_used": False,
            "orders_submitted": False,
        },
        "source": {
            "url": url,
            "filename": filename,
            "published_sha256": published,
            "observed_sha256": observed,
            "rows_total": rows_total,
            "duplicate_adjacent_agg_trade_ids": duplicate_ids,
            "nonmonotonic_agg_trade_ids": nonmonotonic_ids,
        },
        "output": {
            "filename": output_path.name,
            "rows": rows_selected,
            "first_transact_time_ms": first_time,
            "last_transact_time_ms": last_time,
            "sha256": sha256(output_path),
            "bytes": output_path.stat().st_size,
        },
    }
    manifest_path = args.output_dir / f"manifest_{symbol}_{args.month}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive_path.unlink()
    checksum_path.unlink()
    print(json.dumps(manifest, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
