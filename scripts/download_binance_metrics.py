from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = "https://data.binance.vision/data/futures/um/daily/metrics"
SHA_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
FIVE_MINUTES_MS = 300_000
TIME_ALIASES = ("create_time", "timestamp", "time")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    return parser.parse_args()


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def get(url: str, attempts: int = 7) -> bytes:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "smc-ict-metrics-research/2.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            error = exc
        except Exception as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"download failed: {url}: {error}")


def canonical_header(values: Iterable[str]) -> list[str]:
    return [value.lstrip("\ufeff").strip().lower() for value in values]


def timestamp_ms(value: str) -> int:
    text = value.strip()
    try:
        number = int(float(text))
    except ValueError:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    if number >= 10**17:
        return number // 1_000_000
    if number >= 10**14:
        return number // 1_000
    if number >= 10**11:
        return number
    return number * 1_000


def fetch_one(output_root: Path, symbol: str, day: date) -> dict[str, object]:
    day_text = day.isoformat()
    name = f"{symbol}-metrics-{day_text}.zip"
    url = f"{ROOT}/{symbol}/{name}"
    try:
        checksum_payload = get(url + ".CHECKSUM")
        match = SHA_RE.search(checksum_payload.decode("utf-8", errors="strict"))
        if match is None:
            raise ValueError(f"missing SHA-256 in {url}.CHECKSUM")
        expected = match.group(1).lower()
        payload = get(url)
    except FileNotFoundError:
        return {"symbol": symbol, "day": day_text, "status": "missing", "url": url}
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch {url}: {actual} != {expected}")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [item for item in archive.namelist() if item.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"unexpected CSV members {url}: {members}")
        content = archive.read(members[0])
    daily_path = output_root / "daily" / symbol / f"{day_text}.csv"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_bytes(content)
    with io.StringIO(content.decode("utf-8-sig")) as handle:
        header = canonical_header(next(csv.reader(handle), []))
    if not header:
        raise ValueError(f"empty CSV header: {url}")
    return {"symbol": symbol, "day": day_text, "status": "verified", "url": url,
            "official_sha256": expected, "actual_sha256": actual,
            "zip_bytes": len(payload), "csv_bytes": len(content), "header": header}


def normalize_symbol(output_root: Path, symbol: str, records: list[dict[str, object]]) -> dict[str, object]:
    verified = [item for item in records if item["symbol"] == symbol and item["status"] == "verified"]
    if not verified:
        raise RuntimeError(f"no verified metrics archives for {symbol}")
    header_counts = Counter(tuple(item["header"]) for item in verified)
    union: list[str] = []
    for header, _ in header_counts.most_common():
        for name in header:
            if name not in union:
                union.append(name)
    time_col = next((name for name in TIME_ALIASES if name in union), None)
    if time_col is None:
        raise RuntimeError(f"no timestamp column for {symbol}; schemas={list(header_counts)}")
    fieldnames = [time_col] + [name for name in union if name != time_col] + ["source_day"]
    output = output_root / f"{symbol}_metrics_5m.csv.gz"
    rows_written = duplicate_rows = irregular_gaps = missing_bars = 0
    first_ms = last_ms = pending_ms = None
    pending_row: dict[str, str] | None = None
    with gzip.open(output, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in sorted(verified, key=lambda value: str(value["day"])):
            path = output_root / "daily" / symbol / f"{item['day']}.csv"
            with path.open("r", encoding="utf-8-sig", newline="") as source:
                raw_reader = csv.reader(source)
                raw_header = canonical_header(next(raw_reader, []))
                if time_col not in raw_header:
                    raise RuntimeError(f"{path} lacks timestamp {time_col}: {raw_header}")
                for raw in raw_reader:
                    if not raw:
                        continue
                    row = {raw_header[index]: value for index, value in enumerate(raw[:len(raw_header)])}
                    try:
                        ms = timestamp_ms(row[time_col])
                    except (KeyError, ValueError):
                        if row.get(time_col, "").strip().lower() == time_col:
                            continue
                        raise
                    row[time_col] = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
                    row["source_day"] = str(item["day"])
                    if pending_ms is not None and ms < pending_ms:
                        raise RuntimeError(f"non-monotone timestamp in {path}: {ms} < {pending_ms}")
                    if ms == pending_ms:
                        duplicate_rows += 1
                        pending_row = row
                        continue
                    if pending_row is not None and pending_ms is not None:
                        writer.writerow({name: pending_row.get(name, "") for name in fieldnames})
                        rows_written += 1
                        if last_ms is not None:
                            delta = pending_ms - last_ms
                            if delta != FIVE_MINUTES_MS:
                                irregular_gaps += 1
                                missing_bars += delta // FIVE_MINUTES_MS - 1 if delta > FIVE_MINUTES_MS and delta % FIVE_MINUTES_MS == 0 else 1
                        first_ms = pending_ms if first_ms is None else first_ms
                        last_ms = pending_ms
                    pending_ms, pending_row = ms, row
        if pending_row is not None and pending_ms is not None:
            writer.writerow({name: pending_row.get(name, "") for name in fieldnames})
            rows_written += 1
            if last_ms is not None:
                delta = pending_ms - last_ms
                if delta != FIVE_MINUTES_MS:
                    irregular_gaps += 1
                    missing_bars += delta // FIVE_MINUTES_MS - 1 if delta > FIVE_MINUTES_MS and delta % FIVE_MINUTES_MS == 0 else 1
            first_ms = pending_ms if first_ms is None else first_ms
            last_ms = pending_ms
    return {"symbol": symbol, "rows": rows_written,
            "first_time": datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).isoformat(),
            "last_time": datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat(),
            "duplicate_rows_removed": duplicate_rows, "irregular_gap_count": irregular_gaps,
            "missing_bar_count": int(missing_bars), "columns": fieldnames,
            "schema_variants": [{"columns": list(header), "archive_count": count} for header, count in header_counts.most_common()],
            "output_file": output.name, "output_bytes": output.stat().st_size,
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main() -> int:
    args = parse_args()
    if args.end < args.start:
        raise ValueError("--end must not precede --start")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    specs = [(symbol, day) for symbol in args.symbols for day in date_range(args.start, args.end)]
    records: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, args.output_dir, symbol, day): (symbol, day) for symbol, day in specs}
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            records.append(record)
            print(record["status"], record["symbol"], record["day"], flush=True)
    records.sort(key=lambda item: (str(item["symbol"]), str(item["day"])))
    series = [normalize_symbol(args.output_dir, symbol, records) for symbol in args.symbols]
    verified = [item for item in records if item["status"] == "verified"]
    missing = [item for item in records if item["status"] == "missing"]
    manifest = {"source": "Binance Vision USD-M daily metrics archives",
                "start": args.start.isoformat(), "end": args.end.isoformat(),
                "sealed_oos_excluded_from": "2025-08-01T00:00:00+00:00",
                "requested_archives": len(records), "verified_archives": len(verified),
                "coverage_fraction": len(verified) / max(len(records), 1),
                "missing_archives": missing, "archive_records": records,
                "series": series, "no_synthetic_fill": True}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in series:
        print(json.dumps(item, sort_keys=True), flush=True)
    if any(item["rows"] < 100_000 for item in series):
        raise RuntimeError("insufficient normalized metrics rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
