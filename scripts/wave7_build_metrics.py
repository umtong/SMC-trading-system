from __future__ import annotations

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SYMBOLS = ("BTCUSDT", "ETHUSDT")
START = date(2021, 1, 1)
END = date(2025, 7, 31)
ROOT = "https://data.binance.vision/data/futures/um/daily/metrics"
OUT = Path("wave7_metrics")
DAILY = OUT / "daily"
SHA_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def dates():
    current = START
    while current <= END:
        yield current.isoformat()
        current += timedelta(days=1)


def get(url: str, attempts: int = 7) -> bytes:
    error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "smc-ict-wave7-metrics/1.0"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            error = exc
        except Exception as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"download failed: {url}: {error}")


def fetch_one(symbol: str, day: str) -> dict[str, object]:
    name = f"{symbol}-metrics-{day}.zip"
    url = f"{ROOT}/{symbol}/{name}"
    try:
        checksum_payload = get(url + ".CHECKSUM")
        match = SHA_RE.search(checksum_payload.decode("utf-8", errors="strict"))
        if match is None:
            raise ValueError(f"missing checksum: {url}")
        expected = match.group(1).lower()
        payload = get(url)
    except FileNotFoundError:
        return {"symbol": symbol, "day": day, "status": "missing"}
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch {url}: {actual} != {expected}")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [item for item in archive.namelist() if item.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"unexpected members {url}: {members}")
        content = archive.read(members[0])
    path = DAILY / symbol / f"{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "symbol": symbol,
        "day": day,
        "status": "verified",
        "url": url,
        "sha256": actual,
        "bytes": len(payload),
        "csv_bytes": len(content),
    }


def parse_time(value: str) -> int:
    text = value.strip()
    try:
        number = int(float(text))
    except ValueError:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(stamp.timestamp() * 1000)
    if number >= 10**17:
        return number // 1_000_000
    if number >= 10**14:
        return number // 1_000
    if number >= 10**11:
        return number
    return number * 1_000


def build_symbol(symbol: str, records: list[dict[str, object]]) -> dict[str, object]:
    paths = sorted((DAILY / symbol).glob("*.csv"))
    if not paths:
        raise RuntimeError(f"no metrics files for {symbol}")
    output = OUT / f"{symbol}_metrics_5m.csv.gz"
    header = None
    rows = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise RuntimeError(f"no header in {path}")
            if header is None:
                header = list(reader.fieldnames)
            elif list(reader.fieldnames) != header:
                raise RuntimeError(f"header mismatch in {path}: {reader.fieldnames} != {header}")
            rows.extend(dict(row) for row in reader)
    assert header is not None
    time_col = next((name for name in ("create_time", "timestamp", "time") if name in header), None)
    if time_col is None:
        raise RuntimeError(f"no timestamp column for {symbol}: {header}")
    normalized = []
    for row in rows:
        ms = parse_time(row[time_col])
        row[time_col] = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
        normalized.append((ms, row))
    normalized.sort(key=lambda item: item[0])
    deduped = []
    duplicate_rows = 0
    previous = None
    for ms, row in normalized:
        if ms == previous:
            duplicate_rows += 1
            deduped[-1] = (ms, row)
            continue
        deduped.append((ms, row))
        previous = ms
    deltas = [deduped[i][0] - deduped[i - 1][0] for i in range(1, len(deduped))]
    irregular = sum(delta != 300_000 for delta in deltas)
    missing_bars = sum(
        max(delta // 300_000 - 1, 0) if delta % 300_000 == 0 else 1
        for delta in deltas
        if delta != 300_000
    )
    with gzip.open(output, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for _, row in deduped:
            writer.writerow(row)
    return {
        "symbol": symbol,
        "rows": len(deduped),
        "first_time": deduped[0][1][time_col],
        "last_time": deduped[-1][1][time_col],
        "duplicate_rows_removed": duplicate_rows,
        "irregular_gap_count": irregular,
        "missing_bar_count": int(missing_bars),
        "columns": header,
        "output_file": output.name,
        "output_bytes": output.stat().st_size,
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    DAILY.mkdir(parents=True, exist_ok=True)
    specs = [(symbol, day) for symbol in SYMBOLS for day in dates()]
    records: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        futures = {pool.submit(fetch_one, *spec): spec for spec in specs}
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            records.append(record)
            print(record["status"], record["symbol"], record["day"], flush=True)
    records.sort(key=lambda item: (str(item["symbol"]), str(item["day"])))
    series = [build_symbol(symbol, records) for symbol in SYMBOLS]
    verified = [record for record in records if record["status"] == "verified"]
    missing = [record for record in records if record["status"] == "missing"]
    manifest = {
        "source": "Binance Vision USD-M daily metrics archives",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "sealed_oos_excluded_from": "2025-08-01T00:00:00+00:00",
        "requested_archives": len(records),
        "verified_archives": len(verified),
        "missing_archives": missing,
        "archive_records": records,
        "series": series,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    coverage = len(verified) / max(len(records), 1)
    if coverage < 0.95:
        raise RuntimeError(f"metrics coverage too low: {coverage:.4%}")
    print(json.dumps({"coverage": coverage, "series": series}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
