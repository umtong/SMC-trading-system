#!/usr/bin/env python3
"""Build checksum-verified Binance USD-M daily futures-metrics research data.

Only Binance Vision public archives are used. Empty source fields remain missing;
there is no zero fill, forward fill, or future-data imputation. The collector has
no private API, credentials, or order endpoints.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
INTERVAL_MS = 300_000
SOURCE_COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
CANONICAL_COLUMNS = ("create_time_ms", *SOURCE_COLUMNS[1:])


@dataclass(frozen=True)
class DownloadedDay:
    symbol: str
    day: str
    archive_url: str
    zip_path: Path
    published_sha256: str
    observed_sha256: str


@dataclass(frozen=True)
class SourceRecord:
    symbol: str
    day: str
    archive_url: str
    published_sha256: str
    observed_sha256: str
    rows: int
    first_create_time_ms: int | None
    last_create_time_ms: int | None


def day_range(start: str, end: str) -> Iterator[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ValueError("start date is after end date")
    current = first
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path, attempts: int = 6) -> bool:
    """Download one object; False means a stable HTTP 404."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "smc-ict-metrics-research/1.0"}
            )
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(destination)
            return True
        except urllib.error.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            if exc.code == 404:
                return False
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
        if attempt < attempts:
            time.sleep(min(30.0, 2.0**attempt))
    raise RuntimeError(f"download failed: {url}: {last_error}")


def published_checksum(path: Path, filename: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if filename not in text:
        raise ValueError(f"checksum does not name {filename}: {path}")
    candidates = [
        token.lower()
        for token in text.replace("*", " ").split()
        if len(token) == 64
        and all(character in "0123456789abcdefABCDEF" for character in token)
    ]
    if not candidates:
        raise ValueError(f"no SHA-256 in {path}")
    return candidates[0]


def normalize_header(row: Sequence[str]) -> tuple[str, ...]:
    return tuple(item.strip().lower().replace(" ", "_") for item in row)


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
    if value % INTERVAL_MS != 0:
        raise ValueError(f"metrics timestamp is not on the 5m UTC grid: {raw}")
    return value


def fetch_day(symbol: str, day: str, cache_root: Path) -> DownloadedDay | None:
    filename = f"{symbol}-metrics-{day}.zip"
    url = f"{BASE}/{symbol}/{filename}"
    directory = cache_root / symbol
    zip_path = directory / filename
    checksum_path = directory / f"{filename}.CHECKSUM"
    checksum_exists = download(url + ".CHECKSUM", checksum_path)
    archive_exists = download(url, zip_path)
    if checksum_exists != archive_exists:
        raise ValueError(f"archive/checksum availability mismatch: {url}")
    if not archive_exists:
        checksum_path.unlink(missing_ok=True)
        zip_path.unlink(missing_ok=True)
        return None
    published = published_checksum(checksum_path, filename)
    observed = sha256(zip_path)
    checksum_path.unlink(missing_ok=True)
    if published != observed:
        zip_path.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch {filename}: {observed} != {published}")
    return DownloadedDay(symbol, day, url, zip_path, published, observed)


def rows_from_zip(path: Path, expected_symbol: str) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}: {names}")
        with archive.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            try:
                header = normalize_header(next(reader))
            except StopIteration as exc:
                raise ValueError(f"empty metrics archive: {path}") from exc
            if header != SOURCE_COLUMNS:
                raise ValueError(f"unexpected metrics header in {path}: {header}")
            for line_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) != len(SOURCE_COLUMNS):
                    raise ValueError(f"wrong field count {path}:{line_number}: {len(row)}")
                symbol = row[1].strip().upper()
                if symbol != expected_symbol:
                    raise ValueError(f"symbol mismatch {path}:{line_number}: {symbol}")
                timestamp = epoch_ms(row[0])
                output = [str(timestamp), symbol]
                for column, raw_value in zip(SOURCE_COLUMNS[2:], row[2:], strict=True):
                    text = raw_value.strip()
                    if text == "":
                        output.append("")
                        continue
                    value = float(text)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(
                            f"invalid {column} value {path}:{line_number}: {raw_value!r}"
                        )
                    output.append(format(value, ".17g"))
                yield output


def build_symbol(
    symbol: str,
    downloaded: Sequence[DownloadedDay],
    output_dir: Path,
) -> tuple[list[SourceRecord], dict[str, object]]:
    output_path = output_dir / f"{symbol}_metrics_5m.csv.gz"
    sources: list[SourceRecord] = []
    previous: int | None = None
    first: int | None = None
    last: int | None = None
    rows_total = 0
    gap_transitions = 0
    missing_intervals = 0
    duplicate_times = 0
    zero_counts = {column: 0 for column in CANONICAL_COLUMNS[2:]}
    missing_counts = {column: 0 for column in CANONICAL_COLUMNS[2:]}
    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(CANONICAL_COLUMNS)
        for item in sorted(downloaded, key=lambda value: value.day):
            count = 0
            day_first: int | None = None
            day_last: int | None = None
            for row in rows_from_zip(item.zip_path, symbol):
                timestamp = int(row[0])
                if previous is not None:
                    delta = timestamp - previous
                    if delta == 0:
                        duplicate_times += 1
                    if delta <= 0:
                        raise ValueError(f"non-increasing metrics time {symbol}: {timestamp}")
                    if delta != INTERVAL_MS:
                        gap_transitions += 1
                        if delta > INTERVAL_MS and delta % INTERVAL_MS == 0:
                            missing_intervals += delta // INTERVAL_MS - 1
                previous = timestamp
                first = timestamp if first is None else first
                last = timestamp
                day_first = timestamp if day_first is None else day_first
                day_last = timestamp
                for column, value in zip(CANONICAL_COLUMNS[2:], row[2:], strict=True):
                    if value == "":
                        missing_counts[column] += 1
                    else:
                        zero_counts[column] += int(float(value) == 0.0)
                writer.writerow(row)
                rows_total += 1
                count += 1
            sources.append(
                SourceRecord(
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
        "gap_transitions": gap_transitions,
        "missing_5m_intervals": missing_intervals,
        "duplicate_create_times": duplicate_times,
        "zero_counts": zero_counts,
        "missing_counts": missing_counts,
        "missing_value_policy": "source empty fields preserved; no imputation",
        "output": output_path.name,
        "output_sha256": sha256(output_path),
        "output_bytes": output_path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= 64:
        raise ValueError("workers must be in [1, 64]")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_root = output_dir / ".cache"
    symbols = tuple(str(symbol).upper() for symbol in args.symbols)
    days = tuple(day_range(args.start_date, args.end_date))
    requests = [(symbol, day) for symbol in symbols for day in days]
    downloaded: dict[str, list[DownloadedDay]] = {symbol: [] for symbol in symbols}
    missing_days: dict[str, list[str]] = {symbol: [] for symbol in symbols}
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(fetch_day, symbol, day, cache_root): (symbol, day)
            for symbol, day in requests
        }
        for future in as_completed(futures):
            symbol, day = futures[future]
            item = future.result()
            if item is None:
                missing_days[symbol].append(day)
            else:
                downloaded[symbol].append(item)
            completed += 1
            if completed % 100 == 0 or completed == len(requests):
                print(f"downloaded/audited {completed}/{len(requests)}", flush=True)
    sources: list[SourceRecord] = []
    datasets: dict[str, object] = {}
    for symbol in symbols:
        if not downloaded[symbol]:
            raise ValueError(f"no metrics files found for {symbol}")
        records, metadata = build_symbol(symbol, downloaded[symbol], output_dir)
        sources.extend(records)
        metadata["missing_daily_files"] = sorted(missing_days[symbol])
        datasets[symbol] = metadata
    if cache_root.exists():
        shutil.rmtree(cache_root)
    manifest = {
        "contract": {
            "source": "Binance Vision USD-M daily metrics archives",
            "symbols": list(symbols),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "interval": "5m",
            "canonical_columns": list(CANONICAL_COLUMNS),
            "timestamp_policy": "numeric epoch or naive ISO string interpreted as UTC; exact 5m grid required",
            "checksum": "published SHA-256 verified for every included archive",
            "credentials_used": False,
            "orders_submitted": False,
        },
        "datasets": datasets,
        "sources": [asdict(record) for record in sources],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(datasets, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
