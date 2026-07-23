#!/usr/bin/env python3
"""Build a checksum-verified Binance USD-M 15-minute research bundle.

The script downloads Binance Vision monthly archives, verifies the published
SHA-256 checksum for every source archive, normalizes timestamp units to
milliseconds, validates continuity and OHLC invariants, and writes one gzip CSV
per symbol plus a machine-readable source manifest. It never uses private API
credentials and never submits orders.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
SCHEMA = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
)


@dataclass(frozen=True)
class SourceRecord:
    symbol: str
    month: str
    archive_url: str
    checksum_url: str
    published_sha256: str
    observed_sha256: str
    archive_bytes: int
    rows: int
    first_open_time_ms: int | None
    last_open_time_ms: int | None


def month_range(start: str, end: str) -> Iterator[str]:
    sy, sm = (int(part) for part in start.split("-"))
    ey, em = (int(part) for part in end.split("-"))
    if (sy, sm) > (ey, em):
        raise ValueError("start month is after end month")
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            month = 1
            year += 1


def download(url: str, destination: Path, *, attempts: int = 5) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "smc-ict-research-data-audit/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(destination)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt == attempts:
                break
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"download failed after {attempts} attempts: {url}: {last_error}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_published_checksum(path: Path, expected_name: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    fields = text.replace("*", " ").split()
    hashes = [field.lower() for field in fields if len(field) == 64]
    if not hashes:
        raise ValueError(f"no SHA-256 in {path}: {text!r}")
    if expected_name not in text:
        raise ValueError(f"checksum file does not name {expected_name}: {text!r}")
    return hashes[0]


def normalize_epoch_ms(raw: str) -> int:
    value = int(raw)
    # Binance spot switched to microseconds in 2025. USD-M remains milliseconds,
    # but normalize defensively so the data contract is explicit.
    if value >= 10**15:
        value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible epoch value: {raw}")
    return value


def float_value(raw: str, *, name: str) -> float:
    value = float(raw)
    if not (value == value and abs(value) != float("inf")):
        raise ValueError(f"non-finite {name}: {raw}")
    return value


def iter_archive_rows(path: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, found {names}")
        with archive.open(names[0], "r") as raw:
            text = (line.decode("utf-8-sig") for line in raw)
            reader = csv.reader(text)
            for row_number, row in enumerate(reader, start=1):
                if not row:
                    continue
                if row_number == 1 and row[0].strip().lower() in {"open_time", "open time"}:
                    continue
                if len(row) < len(SCHEMA):
                    raise ValueError(f"short row in {path}:{row_number}: {len(row)} fields")
                yield [item.strip() for item in row[: len(SCHEMA)]]


def validate_and_normalize(row: Sequence[str]) -> list[str]:
    output = list(row)
    open_time = normalize_epoch_ms(output[0])
    close_time = normalize_epoch_ms(output[6])
    open_price = float_value(output[1], name="open")
    high = float_value(output[2], name="high")
    low = float_value(output[3], name="low")
    close = float_value(output[4], name="close")
    volume = float_value(output[5], name="volume")
    quote_volume = float_value(output[7], name="quote volume")
    trades = int(float_value(output[8], name="number of trades"))
    taker_buy_base = float_value(output[9], name="taker buy base")
    taker_buy_quote = float_value(output[10], name="taker buy quote")

    if min(open_price, high, low, close) <= 0:
        raise ValueError(f"non-positive OHLC: {row}")
    if high + 1e-12 < max(open_price, close) or low - 1e-12 > min(open_price, close) or high < low:
        raise ValueError(f"invalid OHLC geometry: {row}")
    if min(volume, quote_volume, taker_buy_base, taker_buy_quote) < 0 or trades < 0:
        raise ValueError(f"negative activity field: {row}")
    if taker_buy_base > volume + max(1e-9, volume * 1e-9):
        raise ValueError(f"taker buy base exceeds volume: {row}")
    if close_time < open_time or close_time - open_time > INTERVAL_MS:
        raise ValueError(f"invalid close time: {row}")

    output[0] = str(open_time)
    output[6] = str(close_time)
    output[8] = str(trades)
    return output


def build_symbol(symbol: str, months: Iterable[str], root: Path) -> tuple[list[SourceRecord], dict[str, object]]:
    raw_root = root / "source_archives" / symbol
    output_path = root / f"{symbol}_{INTERVAL}.csv.gz"
    records: list[SourceRecord] = []
    prior_open: int | None = None
    total_rows = 0
    gaps: list[dict[str, int]] = []
    duplicates = 0
    zero_volume = 0
    first_open: int | None = None
    last_open: int | None = None

    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as compressed:
        writer = csv.writer(compressed)
        writer.writerow(SCHEMA)
        for month in months:
            filename = f"{symbol}-{INTERVAL}-{month}.zip"
            archive_url = f"{BASE_URL}/{symbol}/{INTERVAL}/{filename}"
            checksum_url = archive_url + ".CHECKSUM"
            archive_path = raw_root / filename
            checksum_path = raw_root / (filename + ".CHECKSUM")
            print(f"[{symbol}] {month}: download", flush=True)
            download(checksum_url, checksum_path)
            download(archive_url, archive_path)
            published = parse_published_checksum(checksum_path, filename)
            observed = sha256_file(archive_path)
            if observed != published:
                raise ValueError(f"checksum mismatch for {filename}: {observed} != {published}")

            month_rows = 0
            month_first: int | None = None
            month_last: int | None = None
            for raw_row in iter_archive_rows(archive_path):
                row = validate_and_normalize(raw_row)
                open_time = int(row[0])
                if prior_open is not None:
                    delta = open_time - prior_open
                    if delta == 0:
                        duplicates += 1
                        raise ValueError(f"duplicate open time {open_time} in {symbol}")
                    if delta < 0:
                        raise ValueError(f"non-monotonic open time {open_time} after {prior_open}")
                    if delta != INTERVAL_MS:
                        gaps.append({"after_open_time_ms": prior_open, "next_open_time_ms": open_time, "missing_bars": max(0, delta // INTERVAL_MS - 1)})
                prior_open = open_time
                first_open = open_time if first_open is None else first_open
                last_open = open_time
                month_first = open_time if month_first is None else month_first
                month_last = open_time
                if float(row[5]) == 0:
                    zero_volume += 1
                writer.writerow(row)
                total_rows += 1
                month_rows += 1

            records.append(
                SourceRecord(
                    symbol=symbol,
                    month=month,
                    archive_url=archive_url,
                    checksum_url=checksum_url,
                    published_sha256=published,
                    observed_sha256=observed,
                    archive_bytes=archive_path.stat().st_size,
                    rows=month_rows,
                    first_open_time_ms=month_first,
                    last_open_time_ms=month_last,
                )
            )
            archive_path.unlink()
            checksum_path.unlink()

    raw_root.rmdir()
    (root / "source_archives").rmdir() if (root / "source_archives").exists() and not any((root / "source_archives").iterdir()) else None
    return records, {
        "symbol": symbol,
        "interval": INTERVAL,
        "rows": total_rows,
        "first_open_time_ms": first_open,
        "last_open_time_ms": last_open,
        "gaps": gaps,
        "gap_count": len(gaps),
        "missing_bars": sum(int(item["missing_bars"]) for item in gaps),
        "duplicate_open_times": duplicates,
        "zero_volume_bars": zero_volume,
        "output": output_path.name,
        "output_sha256": sha256_file(output_path),
        "output_bytes": output_path.stat().st_size,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--start-month", default="2021-01")
    parser.add_argument("--end-month", default="2026-06")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    months = tuple(month_range(args.start_month, args.end_month))
    sources: list[SourceRecord] = []
    symbols: dict[str, object] = {}
    for symbol in args.symbols:
        records, audit = build_symbol(str(symbol).upper(), months, args.output_dir)
        sources.extend(records)
        symbols[str(symbol).upper()] = audit

    manifest = {
        "contract": {
            "source": "Binance Vision public USD-M futures monthly klines",
            "base_url": BASE_URL,
            "symbols": [str(item).upper() for item in args.symbols],
            "interval": INTERVAL,
            "start_month": args.start_month,
            "end_month": args.end_month,
            "timestamp_unit": "milliseconds",
            "schema": list(SCHEMA),
            "private_credentials_used": False,
            "orders_submitted": False,
        },
        "symbols": symbols,
        "sources": [asdict(item) for item in sources],
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest["symbols"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
