from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

BASE = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"
DATE = "2023-07-03"
DATASETS = (
    ("bookDepth", None),
    ("bookTicker", None),
    ("aggTrades", None),
    ("metrics", None),
    ("klines", "5m"),
)
MAX_DOWNLOAD_BYTES = 160 * 1024 * 1024


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    last: Exception | None = None
    for attempt in range(5):
        try:
            response = session.request(method, url, timeout=(30, 180), allow_redirects=True, **kwargs)
            return response
        except Exception as exc:
            last = exc
            if attempt == 4:
                break
            time.sleep(min(2 ** attempt, 16))
    raise RuntimeError(f"request failed after retries: {url}") from last


def urls(kind: str, interval: str | None) -> tuple[str, str, str]:
    if interval:
        name = f"{SYMBOL}-{interval}-{DATE}.zip"
        url = f"{BASE}/{kind}/{SYMBOL}/{interval}/{name}"
    else:
        name = f"{SYMBOL}-{kind}-{DATE}.zip"
        url = f"{BASE}/{kind}/{SYMBOL}/{name}"
    return name, url, url + ".CHECKSUM"


def parse_checksum(text: str) -> str | None:
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return match.group(1).lower() if match else None


def inspect_csv_zip(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) != 1:
            raise RuntimeError(f"expected exactly one member, got {[m.filename for m in members]}")
        info = members[0]
        first_rows: list[list[str]] = []
        last_row: list[str] | None = None
        row_count = 0
        with archive.open(info) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.reader(text)
            for row in reader:
                if row_count < 5:
                    first_rows.append(row)
                last_row = row
                row_count += 1
        return {
            "member": info.filename,
            "compressed_bytes": int(info.compress_size),
            "uncompressed_bytes": int(info.file_size),
            "row_count_including_header_if_present": row_count,
            "first_rows": first_rows,
            "last_row": last_row,
            "column_count_first_row": len(first_rows[0]) if first_rows else 0,
            "column_count_last_row": len(last_row) if last_row else 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "smc-ict-causal-provenance-probe/1.0"})
    records: list[dict[str, Any]] = []

    for kind, interval in DATASETS:
        name, url, checksum_url = urls(kind, interval)
        record: dict[str, Any] = {
            "market": "futures/um",
            "symbol": SYMBOL,
            "date": DATE,
            "kind": kind,
            "interval": interval,
            "archive_name": name,
            "archive_url": url,
            "checksum_url": checksum_url,
            "strategy_executed": False,
            "candidate_pnl_observed": False,
            "2024_opened": False,
            "2025_opened": False,
            "2026_opened": False,
            "orders_submitted": False,
        }
        head = request(session, "HEAD", url)
        record["head_status"] = int(head.status_code)
        record["head_headers"] = {
            key.lower(): value
            for key, value in head.headers.items()
            if key.lower() in {"content-length", "last-modified", "etag", "content-type", "accept-ranges"}
        }
        checksum_response = request(session, "GET", checksum_url)
        record["checksum_status"] = int(checksum_response.status_code)
        expected = parse_checksum(checksum_response.text) if checksum_response.ok else None
        record["expected_sha256"] = expected
        length = int(head.headers.get("content-length", "0") or 0)
        record["declared_bytes"] = length

        if head.status_code != 200 or expected is None:
            record["downloaded"] = False
            record["reason"] = "archive_or_checksum_unavailable"
            records.append(record)
            continue
        if length and length > MAX_DOWNLOAD_BYTES:
            record["downloaded"] = False
            record["reason"] = "declared_size_exceeds_probe_limit"
            records.append(record)
            continue

        archive_response = request(session, "GET", url)
        record["get_status"] = int(archive_response.status_code)
        if not archive_response.ok:
            record["downloaded"] = False
            record["reason"] = "archive_get_failed"
            records.append(record)
            continue
        payload = archive_response.content
        actual = sha256_bytes(payload)
        record["downloaded"] = True
        record["actual_bytes"] = len(payload)
        record["actual_sha256"] = actual
        record["checksum_match"] = actual == expected
        if actual != expected:
            raise RuntimeError(f"checksum mismatch for {name}: {actual} != {expected}")
        record["zip_inspection"] = inspect_csv_zip(payload)
        records.append(record)

    manifest = {
        "schema_version": 1,
        "purpose": "Official Binance raw-source provenance and schema probe only; no strategy or PnL.",
        "probe_date": DATE,
        "symbol": SYMBOL,
        "max_download_bytes": MAX_DOWNLOAD_BYTES,
        "records": records,
        "all_downloaded_checksums_match": all(
            (not row.get("downloaded")) or row.get("checksum_match") is True for row in records
        ),
        "strategy_executed": False,
        "candidate_pnl_observed": False,
        "2024_opened": False,
        "2025_opened": False,
        "2026_opened": False,
        "orders_submitted": False,
        "paper_or_live_started": False,
    }
    output = args.output / "raw_provenance_probe.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output / "RAW_PROBE_SHA256.txt").write_text(
        f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
