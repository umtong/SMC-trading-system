from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


FUTURES_BASE = "https://data.binance.vision/data/futures/um/monthly"
SPOT_BASE = "https://data.binance.vision/data/spot/monthly"
SERIES = ("spot", "mark", "index", "premium")


@dataclass(frozen=True)
class Month:
    year: int
    month: int

    @property
    def token(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def iter_months(start: str, end: str):
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield Month(year, month)
        month += 1
        if month == 13:
            month = 1
            year += 1


def archive_spec(symbol: str, month: Month, series: str) -> tuple[str, str]:
    name = f"{symbol}-1m-{month.token}.zip"
    if series == "spot":
        folder = f"{SPOT_BASE}/klines/{symbol}/1m"
    elif series == "mark":
        folder = f"{FUTURES_BASE}/markPriceKlines/{symbol}/1m"
    elif series == "index":
        folder = f"{FUTURES_BASE}/indexPriceKlines/{symbol}/1m"
    elif series == "premium":
        folder = f"{FUTURES_BASE}/premiumIndexKlines/{symbol}/1m"
    else:
        raise ValueError(series)
    return f"{folder}/{name}", name


def fetch(url: str, attempts: int = 7) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "easychart-causal-basis/1.1"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"failed to download {url}: {last}")


def normalize_timestamp(value: str) -> int:
    timestamp = int(value)
    # Spot archives switched from milliseconds to microseconds in 2025.
    while timestamp > 10**14:
        timestamp //= 1000
    return timestamp


def parse_klines(payload: bytes) -> dict[int, tuple[float, ...]]:
    output: dict[int, tuple[float, ...]] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise RuntimeError(f"unexpected archive members: {members}")
        with archive.open(members[0]) as source:
            text = io.TextIOWrapper(source, encoding="utf-8", newline="")
            reader = csv.reader(text)
            for row in reader:
                if not row or row[0] == "open_time":
                    continue
                if len(row) < 12:
                    raise RuntimeError(f"unexpected kline row width: {len(row)}")
                timestamp = normalize_timestamp(row[0])
                output[timestamp] = (
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[7]),
                    float(row[10]),
                    float(row[8]),
                )
    return output


def download_one(
    symbol: str,
    month: Month,
    series: str,
) -> tuple[str, dict[int, tuple[float, ...]] | None, dict[str, object]]:
    url, name = archive_spec(symbol, month, series)
    try:
        archive = fetch(url)
        checksum = fetch(f"{url}.CHECKSUM").decode("utf-8").split()[0].lower()
    except FileNotFoundError:
        return series, None, {
            "series": series,
            "month": month.token,
            "url": url,
            "status": "missing",
        }
    actual = hashlib.sha256(archive).hexdigest()
    if actual != checksum:
        raise RuntimeError(
            f"checksum mismatch for {name}: expected {checksum}, got {actual}"
        )
    rows = parse_klines(archive)
    return series, rows, {
        "series": series,
        "month": month.token,
        "url": url,
        "status": "verified",
        "sha256": actual,
        "archive_bytes": len(archive),
        "rows": len(rows),
    }


def safe_basis(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return 10_000.0 * (numerator / denominator - 1.0)


def write_month(
    writer: csv.writer,
    datasets: dict[str, dict[int, tuple[float, ...]]],
) -> tuple[int, int]:
    common = set.intersection(*(set(rows) for rows in datasets.values()))
    if not common:
        return 0, 0
    first_bucket = min(common) // 300_000
    last_bucket = max(common) // 300_000
    count = 0
    skipped = 0
    for bucket_number in range(first_bucket, last_bucket + 1):
        bucket = bucket_number * 300_000
        block = [bucket + offset * 60_000 for offset in range(5)]
        if not all(timestamp in common for timestamp in block):
            skipped += 1
            continue
        spot = [datasets["spot"][timestamp] for timestamp in block]
        mark = [datasets["mark"][timestamp] for timestamp in block]
        index = [datasets["index"][timestamp] for timestamp in block]
        premium = [datasets["premium"][timestamp] for timestamp in block]

        spot_quote = sum(row[4] for row in spot)
        spot_taker_buy_quote = sum(row[5] for row in spot)
        spot_imbalance = (
            0.0
            if spot_quote <= 0.0
            else (2.0 * spot_taker_buy_quote - spot_quote) / spot_quote
        )
        mark_index = [safe_basis(m[3], i[3]) for m, i in zip(mark, index)]
        mark_spot = [safe_basis(m[3], s[3]) for m, s in zip(mark, spot)]
        spot_index = [safe_basis(s[3], i[3]) for s, i in zip(spot, index)]
        writer.writerow(
            [
                bucket,
                spot[0][0],
                max(row[1] for row in spot),
                min(row[2] for row in spot),
                spot[-1][3],
                spot_quote,
                sum(int(row[6]) for row in spot),
                spot_imbalance,
                mark[-1][3],
                index[-1][3],
                premium[0][0],
                max(row[1] for row in premium),
                min(row[2] for row in premium),
                premium[-1][3],
                mark_index[-1],
                sum(mark_index) / 5.0,
                min(mark_index),
                max(mark_index),
                mark_spot[-1],
                sum(mark_spot) / 5.0,
                spot_index[-1],
                sum(spot_index) / 5.0,
            ]
        )
        count += 1
    return count, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", default="2020-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=Path("basis-output"))
    args = parser.parse_args()

    symbol = args.symbol.upper()
    months = tuple(iter_months(args.start, args.end))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / f"{symbol}_spot_perp_basis_5m.csv.gz"
    manifest_rows: list[dict[str, object]] = []
    skipped_months: list[dict[str, object]] = []
    skipped_buckets = 0

    columns = (
        "open_time",
        "spot_open",
        "spot_high",
        "spot_low",
        "spot_close",
        "spot_quote_volume",
        "spot_trade_count",
        "spot_imbalance",
        "mark_close",
        "index_close",
        "premium_open",
        "premium_high",
        "premium_low",
        "premium_close",
        "mark_index_basis_last_bp",
        "mark_index_basis_mean_bp",
        "mark_index_basis_min_bp",
        "mark_index_basis_max_bp",
        "mark_spot_basis_last_bp",
        "mark_spot_basis_mean_bp",
        "spot_index_basis_last_bp",
        "spot_index_basis_mean_bp",
    )
    total_rows = 0
    with gzip.open(target, "wt", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(columns)
        for month in months:
            datasets: dict[str, dict[int, tuple[float, ...]]] = {}
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(args.workers, len(SERIES))
            ) as executor:
                futures = [
                    executor.submit(download_one, symbol, month, series)
                    for series in SERIES
                ]
                for future in concurrent.futures.as_completed(futures):
                    series, rows, manifest = future.result()
                    manifest_rows.append(manifest)
                    if rows is not None:
                        datasets[series] = rows
            missing = sorted(set(SERIES) - set(datasets))
            if missing:
                skipped_months.append({"month": month.token, "missing": missing})
                continue
            written, skipped = write_month(writer, datasets)
            total_rows += written
            skipped_buckets += skipped
            print(
                json.dumps(
                    {
                        "symbol": symbol,
                        "month": month.token,
                        "written_5m": written,
                        "skipped_5m": skipped,
                    }
                ),
                flush=True,
            )
            datasets.clear()

    if total_rows == 0:
        raise RuntimeError("no complete causal five-minute basis rows were produced")
    manifest = {
        "source": "Binance Public Data spot and USD-M monthly one-minute klines",
        "symbol": symbol,
        "start": args.start,
        "end": args.end,
        "causality": (
            "Each output row aggregates exactly five common completed one-minute "
            "rows. Strategy code may expose it only after open_time + 5 minutes."
        ),
        "rows": total_rows,
        "skipped_incomplete_five_minute_buckets": skipped_buckets,
        "skipped_months": skipped_months,
        "columns": columns,
        "normalized_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "archives": sorted(
            manifest_rows,
            key=lambda row: (str(row["month"]), str(row["series"])),
        ),
    }
    manifest_path = args.output_dir / f"{symbol}_spot_perp_basis_5m.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "target": str(target),
                "rows": total_rows,
                "skipped_months": len(skipped_months),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
