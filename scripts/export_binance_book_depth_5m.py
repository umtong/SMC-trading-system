from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import io
import json
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um/daily/bookDepth"
PERCENTAGES = (1, 2, 3, 4, 5)


def iter_dates(year: int):
    current = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while current < end:
        yield current
        current += timedelta(days=1)


def fetch(url: str, attempts: int = 7) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "easychart-causal-book-depth/1.0"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"failed to download {url}: {last}")


def download_day(symbol: str, day: date) -> tuple[date, bytes, dict[str, object]]:
    name = f"{symbol}-bookDepth-{day.isoformat()}.zip"
    folder = f"{BASE}/{symbol}"
    archive = fetch(f"{folder}/{name}")
    checksum = fetch(f"{folder}/{name}.CHECKSUM").decode("utf-8").split()[0].lower()
    actual = hashlib.sha256(archive).hexdigest()
    if checksum != actual:
        raise RuntimeError(
            f"checksum mismatch for {name}: expected {checksum}, got {actual}"
        )
    return day, archive, {
        "date": day.isoformat(),
        "name": name,
        "sha256": actual,
        "bytes": len(archive),
    }


def parse_archive(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise RuntimeError(f"unexpected archive members: {members}")
        with archive.open(members[0]) as source:
            text = io.TextIOWrapper(source, encoding="utf-8", newline="")
            reader = csv.DictReader(text)
            required = {"timestamp", "percentage", "depth", "notional"}
            if set(reader.fieldnames or ()) != required:
                raise RuntimeError(f"unexpected columns: {reader.fieldnames}")
            for row in reader:
                yield (
                    row["timestamp"],
                    int(row["percentage"]),
                    float(row["depth"]),
                    float(row["notional"]),
                )


def aggregate_day(payload: bytes):
    # Binance publishes several percentage levels for each snapshot.  Keep the
    # last complete snapshot in each five-minute bucket.  The downstream
    # strategy must lag the resulting bucket by one completed bar.
    snapshots: dict[str, dict[int, tuple[float, float]]] = defaultdict(dict)
    for timestamp, percentage, depth, notional in parse_archive(payload):
        if percentage == 0 or abs(percentage) not in PERCENTAGES:
            continue
        snapshots[timestamp][percentage] = (depth, notional)

    buckets: dict[str, tuple[str, dict[int, tuple[float, float]]]] = {}
    for timestamp, levels in snapshots.items():
        # Timestamp format is YYYY-MM-DD HH:MM:SS.  Floor minute to 5m.
        minute = int(timestamp[14:16])
        bucket = f"{timestamp[:14]}{minute - minute % 5:02d}:00"
        previous = buckets.get(bucket)
        if previous is None or timestamp > previous[0]:
            buckets[bucket] = (timestamp, levels)

    for bucket in sorted(buckets):
        source_time, levels = buckets[bucket]
        if any(-p not in levels or p not in levels for p in PERCENTAGES):
            continue
        output: list[object] = [bucket, source_time]
        for p in PERCENTAGES:
            bid_depth, bid_notional = levels[-p]
            ask_depth, ask_notional = levels[p]
            output.extend(
                [
                    bid_depth,
                    ask_depth,
                    (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-12),
                    bid_notional,
                    ask_notional,
                    (bid_notional - ask_notional)
                    / max(bid_notional + ask_notional, 1e-12),
                ]
            )
        yield output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--output-dir", type=Path, default=Path("book-depth-output"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    symbol = args.symbol.upper()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(symbol, day) for day in iter_dates(args.year)]
    downloaded: dict[date, bytes] = {}
    manifest_rows: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(download_day, *job) for job in jobs]
        for completed in concurrent.futures.as_completed(futures):
            day, payload, manifest = completed.result()
            downloaded[day] = payload
            manifest_rows.append(manifest)

    columns = ["bucket_time", "source_snapshot_time"]
    for p in PERCENTAGES:
        columns.extend(
            [
                f"bid_depth_{p}pct",
                f"ask_depth_{p}pct",
                f"depth_imbalance_{p}pct",
                f"bid_notional_{p}pct",
                f"ask_notional_{p}pct",
                f"notional_imbalance_{p}pct",
            ]
        )
    target = output_dir / f"{symbol}_book_depth_5m_{args.year}.csv.gz"
    rows = 0
    first_bucket: str | None = None
    last_bucket: str | None = None
    with gzip.open(target, "wt", encoding="utf-8", newline="") as destination:
        writer = csv.writer(destination)
        writer.writerow(columns)
        for day in sorted(downloaded):
            for row in aggregate_day(downloaded[day]):
                writer.writerow(row)
                rows += 1
                first_bucket = first_bucket or str(row[0])
                last_bucket = str(row[0])
            del downloaded[day]

    manifest = {
        "source": "Binance Public Data USD-M daily bookDepth",
        "symbol": symbol,
        "year": args.year,
        "causality": (
            "Each row is the final published snapshot inside a UTC five-minute "
            "bucket. Strategy code must expose it only after that bucket closes."
        ),
        "rows": rows,
        "first_bucket": first_bucket,
        "last_bucket": last_bucket,
        "columns": columns,
        "normalized_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "archives": sorted(manifest_rows, key=lambda row: str(row["date"])),
    }
    manifest_path = output_dir / f"{symbol}_book_depth_5m_{args.year}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"target": str(target), "rows": rows, "manifest": str(manifest_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
