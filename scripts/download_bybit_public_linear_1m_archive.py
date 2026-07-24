from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://public.bybit.com/kline_for_metatrader4"


@dataclass(frozen=True)
class ArchiveSpec:
    symbol: str
    year: int
    month: int

    @property
    def start(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-01"

    @property
    def end(self) -> str:
        day = calendar.monthrange(self.year, self.month)[1]
        return f"{self.year:04d}-{self.month:02d}-{day:02d}"

    @property
    def filename(self) -> str:
        return f"{self.symbol}_1_{self.start}_{self.end}.csv.gz"

    @property
    def url(self) -> str:
        return f"{BASE}/{self.symbol}/{self.year:04d}/{self.filename}"


def iter_months(start: str, end: str) -> Iterable[tuple[int, int]]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def inspect_gzip(blob: bytes) -> dict[str, object]:
    raw = gzip.decompress(blob)  # validates gzip CRC and length
    lines = raw.splitlines()
    if not lines:
        raise ValueError("empty gzip CSV")
    first = lines[0].decode("utf-8", errors="replace")
    delimiter = "\t" if "\t" in first else ","
    fields = [item.strip() for item in first.split(delimiter)]
    return {
        "uncompressed_bytes": len(raw),
        "line_count": len(lines),
        "delimiter": "TAB" if delimiter == "\t" else "COMMA",
        "first_row": fields,
        "column_count": len(fields),
    }


def fetch(session: requests.Session, spec: ArchiveSpec, retries: int) -> tuple[bytes, dict[str, object]]:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(spec.url, timeout=180)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            if not blob.startswith(b"\x1f\x8b"):
                raise ValueError(f"not gzip: {spec.url}")
            inspection = inspect_gzip(blob)
            return blob, {
                **asdict(spec),
                "url": spec.url,
                "filename": spec.filename,
                "bytes": len(blob),
                "sha256": sha256_bytes(blob),
                "attempt": attempt,
                **inspection,
            }
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**attempt, 20))
    assert last is not None
    raise last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start-month", default="2022-01")
    parser.add_argument("--end-month", default="2024-11")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--pause", type=float, default=0.1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "smc-ict-bybit-public-archive-research/1.0"})

    archives: list[dict[str, object]] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for symbol in args.symbols:
        symbol_dir = args.output / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        for year, month in iter_months(args.start_month, args.end_month):
            spec = ArchiveSpec(symbol=symbol, year=year, month=month)
            try:
                blob, metadata = fetch(session, spec, args.retries)
                target = symbol_dir / spec.filename
                target.write_bytes(blob)
                archives.append({**metadata, "path": str(target.relative_to(args.output))})
                print(json.dumps({"ok": spec.filename, "rows": metadata["line_count"]}), flush=True)
            except FileNotFoundError:
                missing.append(spec.url)
                print(json.dumps({"missing": spec.filename}), flush=True)
            except Exception as exc:  # noqa: BLE001
                errors.append({"filename": spec.filename, "error": repr(exc)})
                print(json.dumps({"error": spec.filename, "detail": repr(exc)}), flush=True)
            if args.pause:
                time.sleep(args.pause)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "source_contract": "Bybit public kline_for_metatrader4 monthly 1-minute archive",
        "symbols": args.symbols,
        "start_month": args.start_month,
        "end_month": args.end_month,
        "verification": "HTTPS success, gzip magic, gzip CRC/length decompression, non-empty rows, per-file SHA-256",
        "archives": archives,
        "missing_archives": missing,
        "errors": errors,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"archives": len(archives), "missing": len(missing), "errors": len(errors)}, indent=2))
    return 2 if errors else (3 if not archives else 0)


if __name__ == "__main__":
    raise SystemExit(main())
