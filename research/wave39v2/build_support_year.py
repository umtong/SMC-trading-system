from __future__ import annotations

import argparse
from calendar import isleap
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.request
import zipfile


def fetch(url: str, retries: int) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "wave39-support/2.0"})
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


def csv_rows(payload: bytes):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=7)
    args = parser.parse_args()
    symbol = args.symbol.upper()
    year = args.year
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = "https://data.binance.vision/data/futures/um/monthly"
    sources = []
    contract_rows = []
    funding_rows = []

    for month_number in range(1, 13):
        month = f"{year}-{month_number:02d}"
        for kind in ("klines", "fundingRate"):
            if kind == "klines":
                name = f"{symbol}-1m-{month}.zip"
                url = f"{root}/klines/{symbol}/1m/{name}"
            else:
                name = f"{symbol}-fundingRate-{month}.zip"
                url = f"{root}/fundingRate/{symbol}/{name}"
            expected = checksum(fetch(url + ".CHECKSUM", args.retries))
            payload = fetch(url, args.retries)
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise RuntimeError(f"checksum mismatch: {url}")
            parsed = 0
            for row in csv_rows(payload):
                if not row:
                    continue
                try:
                    if kind == "klines":
                        if len(row) < 12:
                            raise RuntimeError("short kline row")
                        contract_rows.append(
                            [epoch_ms(row[0])] + row[1:6] + [epoch_ms(row[6])] + row[7:12]
                        )
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
                            raise RuntimeError("funding time not found")
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
                        funding_rows.append([time_field[1], rate])
                    parsed += 1
                except ValueError:
                    if parsed == 0:
                        continue
                    raise
            sources.append({
                "kind": kind,
                "month": month,
                "url": url,
                "sha256": actual,
                "bytes": len(payload),
                "rows": parsed,
            })

    contract_rows.sort(key=lambda row: int(row[0]))
    expected_rows = (366 if isleap(year) else 365) * 1440
    if len(contract_rows) != expected_rows:
        raise RuntimeError(f"contract rows {len(contract_rows)} != {expected_rows}")
    if len({int(row[0]) for row in contract_rows}) != len(contract_rows):
        raise RuntimeError("duplicate contract minute")
    for left, right in zip(contract_rows, contract_rows[1:]):
        if int(right[0]) - int(left[0]) != 60_000:
            raise RuntimeError(f"contract clock gap {left[0]}->{right[0]}")
    contract_path = args.output_dir / f"{symbol}_contract_1m_{year}.csv.gz"
    with gzip.open(contract_path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms",
            "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        writer.writerows(contract_rows)

    funding_rows.sort(key=lambda row: int(row[0]))
    unique = []
    seen = set()
    for row in funding_rows:
        timestamp = int(row[0])
        if timestamp in seen:
            continue
        seen.add(timestamp)
        unique.append(row)
    funding_path = args.output_dir / f"{symbol}_funding_{year}.csv.gz"
    with gzip.open(funding_path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time_ms", "funding_rate"])
        writer.writerows(unique)

    manifest = {
        "schema": "wave39-official-support-v2",
        "symbol": symbol,
        "year": year,
        "sources": sources,
        "contract_rows": len(contract_rows),
        "funding_rows": len(unique),
        "outputs": {
            contract_path.name: {"bytes": contract_path.stat().st_size, "sha256": digest(contract_path)},
            funding_path.name: {"bytes": funding_path.stat().st_size, "sha256": digest(funding_path)},
        },
        "future_outcomes_included": False,
        "strategy_results_included": False,
    }
    manifest_path = args.output_dir / f"{symbol}_support_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
