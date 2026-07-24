from __future__ import annotations

import argparse
from calendar import isleap
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.request
import zipfile


FIELDS = (
    "open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms",
    "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote", "ignore",
    "source_present",
)


def fetch(url: str, retries: int) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "wave39-support/3.0"})
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except Exception as exc:
            last = exc
            if attempt + 1 == retries:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"download failed: {url}") from last


def checksum(payload: bytes) -> str:
    token = payload.decode("utf-8-sig").split()[0].lower()
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise RuntimeError("invalid SHA-256 companion")
    return token


def rows_from_zip(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
        if not names:
            raise RuntimeError("archive contains no CSV")
        for name in names:
            with archive.open(name) as binary:
                text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                yield from csv.reader(text)


def epoch_ms(value: str) -> int:
    number = int(float(value))
    if number < 10**11:
        return number * 1000
    if number < 10**14:
        return number
    if number < 10**17:
        return number // 1000
    return number // 1_000_000


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def gap_runs(expected: list[int], present: set[int]) -> list[dict[str, int]]:
    runs: list[dict[str, int]] = []
    start = None
    previous = None
    for timestamp in expected:
        if timestamp not in present:
            if start is None:
                start = timestamp
            previous = timestamp
        elif start is not None:
            assert previous is not None
            runs.append({
                "start_ms": start,
                "end_exclusive_ms": previous + 60_000,
                "missing_minutes": (previous - start) // 60_000 + 1,
            })
            start = None
            previous = None
    if start is not None:
        assert previous is not None
        runs.append({
            "start_ms": start,
            "end_exclusive_ms": previous + 60_000,
            "missing_minutes": (previous - start) // 60_000 + 1,
        })
    return runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=8)
    args = parser.parse_args()
    symbol = args.symbol.upper()
    year = args.year
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = "https://data.binance.vision/data/futures/um/monthly"
    sources = []
    contract_by_time: dict[int, tuple[str, ...]] = {}
    funding_by_time: dict[int, float] = {}

    for month_number in range(1, 13):
        month = f"{year}-{month_number:02d}"
        for kind in ("klines", "fundingRate"):
            if kind == "klines":
                name = f"{symbol}-1m-{month}.zip"
                url = f"{root}/klines/{symbol}/1m/{name}"
            else:
                name = f"{symbol}-fundingRate-{month}.zip"
                url = f"{root}/fundingRate/{symbol}/{name}"
            expected_sha = checksum(fetch(url + ".CHECKSUM", args.retries))
            payload = fetch(url, args.retries)
            actual_sha = hashlib.sha256(payload).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeError(f"checksum mismatch: {url}")
            parsed = 0
            for row in rows_from_zip(payload):
                if not row:
                    continue
                try:
                    if kind == "klines":
                        if len(row) < 12:
                            raise RuntimeError("short kline row")
                        timestamp = epoch_ms(row[0])
                        normalized = tuple(
                            [str(timestamp)] + row[1:6] + [str(epoch_ms(row[6]))] + row[7:12]
                        )
                        previous = contract_by_time.get(timestamp)
                        if previous is not None and previous != normalized:
                            raise RuntimeError(f"conflicting duplicate contract minute {timestamp}")
                        contract_by_time[timestamp] = normalized
                    else:
                        time_field = None
                        for index in (0, 1):
                            try:
                                candidate = epoch_ms(row[index])
                                if 1_500_000_000_000 <= candidate <= 2_000_000_000_000:
                                    time_field = (index, candidate)
                                    break
                            except Exception:
                                pass
                        if time_field is None:
                            if parsed == 0:
                                continue
                            raise RuntimeError("funding timestamp not found")
                        rate = None
                        for index in range(len(row) - 1, -1, -1):
                            if index == time_field[0]:
                                continue
                            try:
                                candidate = float(row[index])
                                if abs(candidate) < 0.1:
                                    rate = candidate
                                    break
                            except Exception:
                                pass
                        if rate is None:
                            raise RuntimeError("funding rate not found")
                        previous_rate = funding_by_time.get(time_field[1])
                        if previous_rate is not None and previous_rate != rate:
                            raise RuntimeError(f"conflicting duplicate funding time {time_field[1]}")
                        funding_by_time[time_field[1]] = rate
                    parsed += 1
                except ValueError:
                    if parsed == 0:
                        continue
                    raise
            sources.append({
                "kind": kind,
                "month": month,
                "url": url,
                "sha256": actual_sha,
                "bytes": len(payload),
                "rows": parsed,
            })

    start_ms = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    expected_clock = list(range(start_ms, end_ms, 60_000))
    if len(expected_clock) != (366 if isleap(year) else 365) * 1440:
        raise RuntimeError("internal expected-clock failure")
    outside = sorted(timestamp for timestamp in contract_by_time if not start_ms <= timestamp < end_ms)
    if outside:
        raise RuntimeError(f"contract rows outside requested year: {outside[:5]}")

    present_times = set(contract_by_time)
    gaps = gap_runs(expected_clock, present_times)
    contract_path = args.output_dir / f"{symbol}_contract_1m_{year}.csv.gz"
    with gzip.open(contract_path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for timestamp in expected_clock:
            row = contract_by_time.get(timestamp)
            if row is None:
                writer.writerow([timestamp, "", "", "", "", "", "", "", "", "", "", "", 0])
                continue
            open_price, high_price, low_price, close_price = map(float, row[1:5])
            if not (
                open_price > 0.0
                and high_price >= max(open_price, close_price, low_price)
                and low_price <= min(open_price, close_price, high_price)
            ):
                raise RuntimeError(f"invalid OHLC row at {timestamp}")
            writer.writerow(list(row) + [1])

    funding_path = args.output_dir / f"{symbol}_funding_{year}.csv.gz"
    with gzip.open(funding_path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time_ms", "funding_rate"])
        for timestamp, rate in sorted(funding_by_time.items()):
            if start_ms <= timestamp < end_ms:
                writer.writerow([timestamp, rate])

    manifest = {
        "schema": "wave39-official-support-v3",
        "symbol": symbol,
        "year": year,
        "sources": sources,
        "expected_contract_minutes": len(expected_clock),
        "observed_contract_minutes": len(present_times),
        "missing_contract_minutes": len(expected_clock) - len(present_times),
        "gap_run_count": len(gaps),
        "largest_gap_minutes": max((item["missing_minutes"] for item in gaps), default=0),
        "gap_runs": gaps,
        "funding_rows": sum(1 for timestamp in funding_by_time if start_ms <= timestamp < end_ms),
        "gap_policy": "No OHLCV value is imputed. Missing minutes are explicit rows with source_present=0 and blank market fields; every signal whose history or holding path intersects one is invalidated downstream.",
        "outputs": {
            contract_path.name: {"bytes": contract_path.stat().st_size, "sha256": digest(contract_path)},
            funding_path.name: {"bytes": funding_path.stat().st_size, "sha256": digest(funding_path)},
        },
        "future_outcomes_included": False,
        "strategy_results_included": False,
    }
    manifest_path = args.output_dir / f"{symbol}_support_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "symbol": symbol,
        "year": year,
        "observed_contract_minutes": manifest["observed_contract_minutes"],
        "missing_contract_minutes": manifest["missing_contract_minutes"],
        "gap_run_count": manifest["gap_run_count"],
        "largest_gap_minutes": manifest["largest_gap_minutes"],
        "funding_rows": manifest["funding_rows"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
