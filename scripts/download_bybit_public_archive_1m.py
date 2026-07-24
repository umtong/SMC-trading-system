from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import gzip
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

BASE = "https://public.bybit.com/kline_for_metatrader4"


@dataclass(frozen=True)
class Spec:
    symbol: str
    year: int
    month: int

    @property
    def start(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-01"

    @property
    def end(self) -> str:
        last = calendar.monthrange(self.year, self.month)[1]
        return f"{self.year:04d}-{self.month:02d}-{last:02d}"

    @property
    def filename(self) -> str:
        return f"{self.symbol}_1_{self.start}_{self.end}.csv.gz"

    @property
    def url(self) -> str:
        return f"{BASE}/{self.symbol}/{self.year}/{self.filename}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(spec: Spec, retries: int = 6) -> tuple[Spec, bytes]:
    headers = {"User-Agent": "smc-ict-crossvenue-archive/1.0"}
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=180)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            return spec, response.content
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**attempt, 20))
    assert last is not None
    raise last


def inspect(blob: bytes) -> dict[str, object]:
    raw = gzip.decompress(blob)  # validates gzip header, CRC and uncompressed size
    if not raw:
        raise ValueError("empty decompressed archive")
    lines = raw.splitlines()
    first = lines[0].decode("utf-8", errors="replace")
    return {
        "compressed_bytes": len(blob),
        "compressed_sha256": sha256_bytes(blob),
        "uncompressed_bytes": len(raw),
        "uncompressed_sha256": sha256_bytes(raw),
        "line_count": len(lines),
        "header_or_first_row": first[:500],
    }


def months(start: str, end: str) -> list[tuple[int, int]]:
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    result: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append((y, m))
        m += 1
        if m == 13:
            y += 1
            m = 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start-month", default="2022-01")
    parser.add_argument("--end-month", default="2024-11")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    specs = [
        Spec(symbol, year, month)
        for year, month in months(args.start_month, args.end_month)
        for symbol in args.symbols
    ]
    datasets: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    errors: list[dict[str, str]] = []

    def work(spec: Spec) -> tuple[Spec, bytes, dict[str, object]]:
        _, blob = fetch(spec)
        return spec, blob, inspect(blob)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, spec): spec for spec in specs}
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            key = f"{spec.symbol}_{spec.year:04d}_{spec.month:02d}"
            try:
                _, blob, info = future.result()
                path = args.output / spec.filename
                path.write_bytes(blob)
                datasets[key] = {
                    **asdict(spec),
                    "url": spec.url,
                    "path": path.name,
                    **info,
                }
                print(json.dumps({"ok": key, "bytes": len(blob)}), flush=True)
            except FileNotFoundError:
                missing.append(spec.url)
                print(json.dumps({"missing": key}), flush=True)
            except Exception as exc:  # noqa: BLE001
                errors.append({"key": key, "error": repr(exc)})
                print(json.dumps({"error": key, "detail": repr(exc)}), flush=True)

    manifest = {
        "source": BASE,
        "symbols": args.symbols,
        "start_month": args.start_month,
        "end_month": args.end_month,
        "availability_contract": "monthly public archive; only fully downloaded and gzip-validated files are usable",
        "requested": len(specs),
        "datasets": dict(sorted(datasets.items())),
        "missing_archives": sorted(missing),
        "errors": errors,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if errors or missing or len(datasets) != len(specs):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
