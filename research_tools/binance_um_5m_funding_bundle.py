#!/usr/bin/env python3
"""Build checksum-verified Binance USD-M 5m kline and funding bundles.

Public research data only. No credentials and no order endpoints.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

ROOT = "https://data.binance.vision/data/futures/um/monthly"
KLINE_SCHEMA = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_asset_volume", "number_of_trades", "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume", "ignore",
)
INTERVAL = "5m"
INTERVAL_MS = 300_000


@dataclass(frozen=True)
class Source:
    symbol: str
    data_type: str
    month: str
    archive_url: str
    published_sha256: str
    observed_sha256: str
    rows: int
    first_time_ms: int | None
    last_time_ms: int | None


def months(start: str, end: str) -> Iterator[str]:
    sy, sm = map(int, start.split("-")); ey, em = map(int, end.split("-"))
    if (sy, sm) > (ey, em):
        raise ValueError("start after end")
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def get(url: str, path: Path, attempts: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smc-ict-public-data-audit/1.0"})
            with urllib.request.urlopen(req, timeout=180) as response, tmp.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            tmp.replace(path)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc; tmp.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"download failed: {url}: {error}")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def published(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if name not in text:
        raise ValueError(f"checksum does not name {name}")
    values = [part.lower() for part in text.replace("*", " ").split() if len(part) == 64]
    if not values:
        raise ValueError(f"no checksum in {path}")
    return values[0]


def epoch_ms(raw: str) -> int:
    value = int(raw)
    if value >= 10**15:
        value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible timestamp: {raw}")
    return value


def archive_rows(path: Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as zf:
        names = [name for name in zf.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}: {names}")
        with zf.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            for row in reader:
                if row:
                    yield [item.strip() for item in row]


def fetch_archive(base: str, filename: str, cache: Path) -> tuple[Path, str, str, str]:
    url = f"{base}/{filename}"
    archive = cache / filename
    check = cache / f"{filename}.CHECKSUM"
    get(url + ".CHECKSUM", check); get(url, archive)
    expected = published(check, filename); observed = digest(archive)
    if expected != observed:
        raise ValueError(f"checksum mismatch {filename}: {observed} != {expected}")
    return archive, expected, observed, url


def build_klines(symbol: str, month_values: Iterable[str], root: Path) -> tuple[list[Source], dict[str, object]]:
    output = root / f"{symbol}_{INTERVAL}.csv.gz"
    cache = root / ".cache" / symbol / "klines"
    sources: list[Source] = []
    prior: int | None = None; first: int | None = None; last: int | None = None
    rows_total = 0; gaps = 0; zero_volume = 0
    with gzip.open(output, "wt", encoding="utf-8", newline="", compresslevel=6) as gz:
        writer = csv.writer(gz); writer.writerow(KLINE_SCHEMA)
        for month in month_values:
            filename = f"{symbol}-{INTERVAL}-{month}.zip"
            base = f"{ROOT}/klines/{symbol}/{INTERVAL}"
            archive, expected, observed, url = fetch_archive(base, filename, cache)
            count = 0; month_first = None; month_last = None
            for n, row in enumerate(archive_rows(archive), start=1):
                if n == 1 and row[0].lower().replace(" ", "_") == "open_time":
                    continue
                if len(row) < 12:
                    raise ValueError(f"short kline row {filename}:{n}")
                row = row[:12]
                opened = epoch_ms(row[0]); closed = epoch_ms(row[6])
                o, h, l, c = map(float, row[1:5]); volume = float(row[5])
                qv = float(row[7]); trades = int(float(row[8])); buy = float(row[9]); buyq = float(row[10])
                if min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c) or h < l:
                    raise ValueError(f"invalid OHLC {filename}:{n}")
                if min(volume, qv, buy, buyq) < 0 or trades < 0 or buy > volume + max(1e-9, volume * 1e-9):
                    raise ValueError(f"invalid activity {filename}:{n}")
                if closed - opened != INTERVAL_MS - 1:
                    raise ValueError(f"invalid interval clock {filename}:{n}")
                if prior is not None:
                    delta = opened - prior
                    if delta <= 0:
                        raise ValueError(f"duplicate/nonmonotonic {symbol} {opened}")
                    if delta != INTERVAL_MS:
                        gaps += 1
                prior = opened; first = opened if first is None else first; last = opened
                month_first = opened if month_first is None else month_first; month_last = opened
                zero_volume += int(volume == 0)
                row[0] = str(opened); row[6] = str(closed); row[8] = str(trades)
                writer.writerow(row); count += 1; rows_total += 1
            sources.append(Source(symbol, "klines_5m", month, url, expected, observed, count, month_first, month_last))
            archive.unlink(); (cache / f"{filename}.CHECKSUM").unlink()
    return sources, {
        "rows": rows_total, "first_time_ms": first, "last_time_ms": last,
        "gap_transitions": gaps, "zero_volume_bars": zero_volume,
        "output": output.name, "sha256": digest(output), "bytes": output.stat().st_size,
    }


def build_funding(symbol: str, month_values: Iterable[str], root: Path) -> tuple[list[Source], dict[str, object]]:
    output = root / f"{symbol}_fundingRate.csv.gz"
    cache = root / ".cache" / symbol / "fundingRate"
    sources: list[Source] = []
    expected_header: list[str] | None = None
    prior: int | None = None; first: int | None = None; last: int | None = None
    rows_total = 0; duplicates = 0
    with gzip.open(output, "wt", encoding="utf-8", newline="", compresslevel=6) as gz:
        writer = csv.writer(gz)
        for month in month_values:
            filename = f"{symbol}-fundingRate-{month}.zip"
            base = f"{ROOT}/fundingRate/{symbol}"
            archive, expected, observed, url = fetch_archive(base, filename, cache)
            iterator = iter(archive_rows(archive))
            try:
                first_row = next(iterator)
            except StopIteration:
                first_row = []
            has_header = bool(first_row) and not first_row[0].lstrip("-+").isdigit()
            header = first_row if has_header else ["calc_time", "funding_interval_hours", "last_funding_rate"][:len(first_row)]
            data_rows = iterator if has_header else iter(([first_row] if first_row else []) + list(iterator))
            normalized_header = [item.strip().lower().replace(" ", "_") for item in header]
            if expected_header is None:
                expected_header = normalized_header
                writer.writerow(expected_header)
            elif normalized_header != expected_header:
                raise ValueError(f"funding header changed in {filename}: {normalized_header} != {expected_header}")
            count = 0; month_first = None; month_last = None
            for n, row in enumerate(data_rows, start=2 if has_header else 1):
                if len(row) != len(expected_header):
                    raise ValueError(f"funding field count {filename}:{n}")
                timestamp = epoch_ms(row[0])
                if prior is not None and timestamp <= prior:
                    if timestamp == prior:
                        duplicates += 1
                    raise ValueError(f"duplicate/nonmonotonic funding {symbol} {timestamp}")
                # Validate every remaining value is numeric but preserve exact source text.
                for value in row[1:]:
                    float(value)
                prior = timestamp; first = timestamp if first is None else first; last = timestamp
                month_first = timestamp if month_first is None else month_first; month_last = timestamp
                row[0] = str(timestamp); writer.writerow(row); count += 1; rows_total += 1
            sources.append(Source(symbol, "fundingRate", month, url, expected, observed, count, month_first, month_last))
            archive.unlink(); (cache / f"{filename}.CHECKSUM").unlink()
    return sources, {
        "header": expected_header, "rows": rows_total, "first_time_ms": first,
        "last_time_ms": last, "duplicates": duplicates, "output": output.name,
        "sha256": digest(output), "bytes": output.stat().st_size,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=("BTCUSDT", "ETHUSDT"))
    p.add_argument("--start-month", default="2023-01")
    p.add_argument("--end-month", default="2026-06")
    p.add_argument("--output-dir", type=Path, required=True)
    return p.parse_args()


def main() -> int:
    args = parse_args(); args.output_dir.mkdir(parents=True, exist_ok=True)
    month_values = tuple(months(args.start_month, args.end_month))
    all_sources: list[Source] = []; symbols: dict[str, object] = {}
    for raw_symbol in args.symbols:
        symbol = raw_symbol.upper()
        print(f"[{symbol}] 5m klines", flush=True)
        k_sources, k_meta = build_klines(symbol, month_values, args.output_dir)
        print(f"[{symbol}] funding", flush=True)
        f_sources, f_meta = build_funding(symbol, month_values, args.output_dir)
        all_sources.extend(k_sources); all_sources.extend(f_sources)
        symbols[symbol] = {"klines_5m": k_meta, "fundingRate": f_meta}
    cache = args.output_dir / ".cache"
    if cache.exists(): shutil.rmtree(cache)
    manifest = {
        "contract": {
            "source": "Binance Vision public USD-M monthly archives",
            "symbols": [s.upper() for s in args.symbols], "start_month": args.start_month,
            "end_month": args.end_month, "kline_interval": INTERVAL,
            "credentials_used": False, "orders_submitted": False,
        },
        "symbols": symbols,
        "sources": [asdict(item) for item in all_sources],
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(symbols, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
