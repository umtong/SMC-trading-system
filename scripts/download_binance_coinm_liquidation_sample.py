from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

BASE = "https://data.binance.vision/data/futures/cm/daily/liquidationSnapshot"


@dataclass(frozen=True)
class Spec:
    symbol: str
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-liquidationSnapshot-{self.day}.zip"

    @property
    def url(self) -> str:
        return f"{BASE}/{self.symbol}/{self.filename}"


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def parse_checksum(text: str) -> str:
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if match is None:
        raise ValueError(f"invalid checksum payload: {text[:120]!r}")
    return match.group(1).lower()


def fetch(spec: Spec, retries: int = 4) -> tuple[bytes, dict[str, object]]:
    headers = {"User-Agent": "smc-ict-liquidation-research/1.0"}
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=180)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            checksum = requests.get(spec.url + ".CHECKSUM", headers=headers, timeout=60)
            checksum.raise_for_status()
            expected = parse_checksum(checksum.text)
            actual = sha256_bytes(blob)
            if actual != expected:
                raise ValueError(f"checksum mismatch: {actual} != {expected}")
            return blob, {
                **asdict(spec),
                "url": spec.url,
                "filename": spec.filename,
                "bytes": len(blob),
                "sha256": actual,
                "attempt": attempt,
            }
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**attempt, 15))
    assert last is not None
    raise last


def inspect_and_export(blob: bytes, target: Path) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV member, found {names}")
        raw = archive.read(names[0])
    lines = raw.splitlines()
    if not lines:
        raise ValueError("empty CSV member")
    first = lines[0].decode("utf-8", errors="replace")
    has_header = any(ch.isalpha() for ch in first)
    sample_rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(raw.decode("utf-8", errors="replace")))
    for index, row in enumerate(reader):
        if index >= 6:
            break
        sample_rows.append(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb", compresslevel=6, mtime=0) as handle:
        handle.write(raw)
    return {
        "member": names[0],
        "rows": max(0, len(lines) - (1 if has_header else 0)),
        "column_count": len(sample_rows[0]) if sample_rows else 0,
        "has_header": has_header,
        "header_or_first_row": sample_rows[0] if sample_rows else [],
        "sample_rows": sample_rows[:4],
        "output": target.name,
        "output_bytes": target.stat().st_size,
        "output_sha256": sha256_bytes(target.read_bytes()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSD_PERP", "ETHUSD_PERP"])
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    datasets: dict[str, dict[str, object]] = {}
    archives: list[dict[str, object]] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for day in args.dates:
        for symbol in args.symbols:
            spec = Spec(symbol, day)
            key = f"{symbol}_{day}"
            try:
                blob, meta = fetch(spec)
                info = inspect_and_export(blob, args.output / f"{key}.csv.gz")
                archives.append(meta)
                datasets[key] = info
                print(json.dumps({"ok": key, "rows": info["rows"]}), flush=True)
            except FileNotFoundError:
                missing.append(spec.url)
                print(json.dumps({"missing": key}), flush=True)
            except Exception as exc:  # noqa: BLE001
                errors.append({"key": key, "error": repr(exc)})
                print(json.dumps({"error": key, "detail": repr(exc)}), flush=True)

    manifest = {
        "source": BASE,
        "symbols": args.symbols,
        "dates": args.dates,
        "archives": archives,
        "datasets": datasets,
        "missing_archives": missing,
        "errors": errors,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if errors:
        return 2
    if not datasets:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
