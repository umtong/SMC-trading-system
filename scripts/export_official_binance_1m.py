from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import json
import time
import urllib.request
import zipfile
from pathlib import Path

HEADER = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"


def month_range(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def download(url: str, path: Path, *, attempts: int = 6) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "easychart-causal-research/1.0"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = response.read()
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"failed to download {url}: {last_error}")


def verify_archive(
    symbol: str,
    year: int,
    month: int,
    cache: Path,
) -> dict[str, object]:
    name = f"{symbol}-1m-{year:04d}-{month:02d}.zip"
    folder = f"{BASE_URL}/{symbol}/1m"
    archive = cache / name
    checksum = cache / f"{name}.CHECKSUM"
    download(f"{folder}/{name}", archive)
    download(f"{folder}/{name}.CHECKSUM", checksum)

    expected = checksum.read_text(encoding="utf-8").split()[0].lower()
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"checksum mismatch for {name}: expected {expected}, got {actual}"
        )

    with zipfile.ZipFile(archive) as source:
        members = [name for name in source.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise RuntimeError(f"unexpected archive members in {name}: {members}")
        with source.open(members[0]) as rows:
            first = rows.readline().decode("utf-8").strip().split(",")
        if len(first) != len(HEADER):
            raise RuntimeError(f"unexpected column count in {name}: {len(first)}")

    return {
        "symbol": symbol,
        "year": year,
        "month": month,
        "archive": str(archive),
        "sha256": actual,
        "bytes": archive.stat().st_size,
    }


def normalize_symbol(
    symbol: str,
    start: tuple[int, int],
    end: tuple[int, int],
    cache: Path,
    output: Path,
) -> dict[str, object]:
    destination = output / f"{symbol}_1m_full.csv.gz"
    rows = 0
    first_open: int | None = None
    last_open: int | None = None
    previous_open: int | None = None

    with gzip.open(destination, "wt", encoding="utf-8", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(HEADER)
        for year, month in month_range(start, end):
            archive = cache / f"{symbol}-1m-{year:04d}-{month:02d}.zip"
            with zipfile.ZipFile(archive) as source:
                member = [name for name in source.namelist() if not name.endswith("/")][0]
                with source.open(member) as compressed_rows:
                    reader = csv.reader(
                        line.decode("utf-8") for line in compressed_rows
                    )
                    for row in reader:
                        if not row or row[0] == "open_time":
                            continue
                        if len(row) != len(HEADER):
                            raise RuntimeError(
                                f"unexpected row width for {symbol} {year}-{month:02d}"
                            )
                        open_time = int(row[0])
                        if (
                            previous_open is not None
                            and open_time - previous_open != 60_000
                        ):
                            raise RuntimeError(
                                f"non-contiguous interval for {symbol}: "
                                f"{previous_open} -> {open_time}"
                            )
                        if first_open is None:
                            first_open = open_time
                        previous_open = open_time
                        last_open = open_time
                        writer.writerow(row)
                        rows += 1

    return {
        "path": str(destination),
        "rows": rows,
        "first_open_time_ms": first_open,
        "last_open_time_ms": last_open,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "bytes": destination.stat().st_size,
    }


def parse_month(value: str) -> tuple[int, int]:
    year, month = value.split("-", maxsplit=1)
    result = int(year), int(month)
    if result[1] < 1 or result[1] > 12:
        raise argparse.ArgumentTypeError(f"invalid month: {value}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download official Binance USD-M monthly one-minute kline archives, "
            "verify every published SHA-256 checksum, and build continuous files."
        )
    )
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", type=parse_month, default=(2022, 1))
    parser.add_argument("--end", type=parse_month, default=(2026, 6))
    parser.add_argument("--output-dir", type=Path, default=Path("rich-data"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    root = args.output_dir
    cache = root / "archives"
    normalized = root / "normalized"
    cache.mkdir(parents=True, exist_ok=True)
    normalized.mkdir(parents=True, exist_ok=True)

    jobs = [
        (symbol, year, month, cache)
        for symbol in args.symbols
        for year, month in month_range(args.start, args.end)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        archives = list(executor.map(lambda values: verify_archive(*values), jobs))

    outputs = {
        symbol: normalize_symbol(
            symbol,
            args.start,
            args.end,
            cache,
            normalized,
        )
        for symbol in args.symbols
    }
    manifest = {
        "source": "Binance Public Data USD-M monthly klines",
        "source_url": BASE_URL,
        "market": "futures/um",
        "interval": "1m",
        "start_month": f"{args.start[0]:04d}-{args.start[1]:02d}",
        "end_month": f"{args.end[0]:04d}-{args.end[1]:02d}",
        "symbols": list(args.symbols),
        "columns": list(HEADER),
        "archives": sorted(
            archives,
            key=lambda row: (
                str(row["symbol"]),
                int(row["year"]),
                int(row["month"]),
            ),
        ),
        "normalized": outputs,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"manifest": str(manifest_path), "normalized": outputs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
