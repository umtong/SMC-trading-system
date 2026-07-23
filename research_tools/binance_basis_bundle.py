#!/usr/bin/env python3
"""Build checksum-verified Binance 5m spot/perpetual basis research data.

Downloads public Binance Vision monthly archives for spot, USD-M mark price,
index price, and premium index klines. No credentials or order endpoints.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Sequence

INTERVAL = "5m"
INTERVAL_MS = 300_000
SCHEMA = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_asset_volume", "number_of_trades", "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume", "ignore",
)


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    path_prefix: str
    output_suffix: str
    price_must_be_positive: bool
    validate_activity: bool


@dataclass(frozen=True)
class SourceRecord:
    symbol: str
    dataset: str
    month: str
    archive_url: str
    published_sha256: str
    observed_sha256: str
    rows: int
    first_open_time_ms: int | None
    last_open_time_ms: int | None


SPECS = (
    DatasetSpec("spot", "spot/monthly/klines", "spot_5m", True, True),
    DatasetSpec("markPrice", "futures/um/monthly/markPriceKlines", "markPrice_5m", True, False),
    DatasetSpec("indexPrice", "futures/um/monthly/indexPriceKlines", "indexPrice_5m", True, False),
    DatasetSpec("premiumIndex", "futures/um/monthly/premiumIndexKlines", "premiumIndex_5m", False, False),
)
BASE = "https://data.binance.vision/data"


def month_range(start: str, end: str) -> Iterator[str]:
    sy, sm = map(int, start.split("-")); ey, em = map(int, end.split("-"))
    if (sy, sm) > (ey, em):
        raise ValueError("start after end")
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year, month = year + 1, 1


def download(url: str, destination: Path, attempts: int = 5) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "smc-ict-basis-research/1.0"})
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(destination)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc; temporary.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"download failed: {url}: {last}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def published_checksum(path: Path, filename: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if filename not in text:
        raise ValueError(f"checksum does not name {filename}")
    candidates = [part.lower() for part in text.replace("*", " ").split() if len(part) == 64]
    if not candidates:
        raise ValueError(f"missing checksum in {path}")
    return candidates[0]


def epoch_ms(raw: str) -> int:
    value = int(raw)
    # Binance spot archives use microseconds from 2025 onward.
    if value >= 10**15:
        value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible epoch: {raw}")
    return value


def rows_from_zip(path: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}: {names}")
        with archive.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            for row in reader:
                if row:
                    yield [item.strip() for item in row]


def normalize_row(row: Sequence[str], spec: DatasetSpec, filename: str, line_number: int) -> list[str]:
    if len(row) < 12:
        raise ValueError(f"short row {filename}:{line_number}")
    output = list(row[:12])
    opened = epoch_ms(output[0]); closed = epoch_ms(output[6])
    o, h, l, c = map(float, output[1:5])
    if not all(math.isfinite(value) for value in (o, h, l, c)):
        raise ValueError(f"non-finite OHLC {filename}:{line_number}")
    if spec.price_must_be_positive and min(o, h, l, c) <= 0:
        raise ValueError(f"non-positive price {filename}:{line_number}")
    if h + 1e-15 < max(o, c) or l - 1e-15 > min(o, c) or h < l:
        raise ValueError(f"invalid OHLC geometry {filename}:{line_number}")
    if closed - opened != INTERVAL_MS - 1:
        raise ValueError(f"invalid clock {filename}:{line_number}: {closed-opened}")
    numeric = [float(output[index]) for index in (5, 7, 9, 10)]
    trades = int(float(output[8]))
    if not all(math.isfinite(value) for value in numeric) or trades < 0:
        raise ValueError(f"invalid activity {filename}:{line_number}")
    if spec.validate_activity:
        volume, quote_volume, taker_base, taker_quote = numeric
        if min(volume, quote_volume, taker_base, taker_quote) < 0:
            raise ValueError(f"negative activity {filename}:{line_number}")
        if taker_base > volume + max(1e-9, abs(volume) * 1e-9):
            raise ValueError(f"taker base exceeds volume {filename}:{line_number}")
    output[0] = str(opened); output[6] = str(closed); output[8] = str(trades)
    return output


def build_dataset(symbol: str, spec: DatasetSpec, months: tuple[str, ...], root: Path) -> tuple[list[SourceRecord], dict[str, object]]:
    output = root / f"{symbol}_{spec.output_suffix}.csv.gz"
    cache = root / ".cache" / spec.key / symbol
    sources: list[SourceRecord] = []
    prior: int | None = None; first: int | None = None; last: int | None = None
    rows_total = 0; gaps = 0; duplicates = 0; zero_prices = 0
    with gzip.open(output, "wt", encoding="utf-8", newline="", compresslevel=6) as compressed:
        writer = csv.writer(compressed); writer.writerow(SCHEMA)
        for month in months:
            filename = f"{symbol}-{INTERVAL}-{month}.zip"
            base = f"{BASE}/{spec.path_prefix}/{symbol}/{INTERVAL}"
            url = f"{base}/{filename}"
            archive_path = cache / filename; checksum_path = cache / f"{filename}.CHECKSUM"
            print(f"[{spec.key}:{symbol}] {month}", flush=True)
            download(url + ".CHECKSUM", checksum_path); download(url, archive_path)
            published = published_checksum(checksum_path, filename); observed = sha256(archive_path)
            if published != observed:
                raise ValueError(f"checksum mismatch {filename}: {observed} != {published}")
            count = 0; month_first = None; month_last = None
            for line_number, raw in enumerate(rows_from_zip(archive_path), start=1):
                if line_number == 1 and raw[0].strip().lower().replace(" ", "_") == "open_time":
                    continue
                row = normalize_row(raw, spec, filename, line_number)
                opened = int(row[0])
                if prior is not None:
                    delta = opened - prior
                    if delta == 0:
                        duplicates += 1
                    if delta <= 0:
                        raise ValueError(f"duplicate/nonmonotonic {spec.key}:{symbol}:{opened}")
                    if delta != INTERVAL_MS:
                        gaps += 1
                prior = opened; first = opened if first is None else first; last = opened
                month_first = opened if month_first is None else month_first; month_last = opened
                zero_prices += int(float(row[4]) == 0.0)
                writer.writerow(row); count += 1; rows_total += 1
            sources.append(SourceRecord(symbol, spec.key, month, url, published, observed, count, month_first, month_last))
            archive_path.unlink(); checksum_path.unlink()
    return sources, {
        "rows": rows_total, "first_open_time_ms": first, "last_open_time_ms": last,
        "gap_transitions": gaps, "duplicate_open_times": duplicates,
        "zero_close_values": zero_prices, "output": output.name,
        "output_sha256": sha256(output), "output_bytes": output.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--start-month", default="2023-01")
    parser.add_argument("--end-month", default="2026-06")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    months = tuple(month_range(args.start_month, args.end_month))
    records: list[SourceRecord] = []; datasets: dict[str, object] = {}
    for spec in SPECS:
        datasets[spec.key] = {}
        for raw_symbol in args.symbols:
            symbol = raw_symbol.upper()
            source_rows, metadata = build_dataset(symbol, spec, months, args.output_dir)
            records.extend(source_rows); datasets[spec.key][symbol] = metadata
    cache = args.output_dir / ".cache"
    if cache.exists(): shutil.rmtree(cache)
    manifest = {
        "contract": {
            "source": "Binance Vision public monthly archives",
            "symbols": [str(symbol).upper() for symbol in args.symbols],
            "start_month": args.start_month, "end_month": args.end_month,
            "interval": INTERVAL, "datasets": [spec.key for spec in SPECS],
            "spot_timestamp_normalization": "microseconds to milliseconds when >=1e15",
            "credentials_used": False, "orders_submitted": False,
        },
        "datasets": datasets,
        "sources": [asdict(record) for record in records],
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(datasets, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
