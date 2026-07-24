from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
COLS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]
HORIZONS_MIN = (10, 30, 60, 240)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    value = year * 12 + month - 1 + delta
    return value // 12, value % 12 + 1


def get(url: str, attempts: int = 7) -> bytes:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "smc-ofi-dollar-bars/1.0"},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except Exception as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"download failed {url}: {error!r}")


def ensure_archive(cache: Path, symbol: str, year: int, month: int) -> Path:
    month_key = f"{year:04d}-{month:02d}"
    name = f"{symbol}-aggTrades-{month_key}.zip"
    folder = cache / symbol
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    checksum_path = folder / f"{name}.CHECKSUM"
    url = f"{BASE}/{symbol}/{name}"
    if not checksum_path.exists():
        checksum_path.write_bytes(get(url + ".CHECKSUM"))
    match = re.search(rb"([0-9a-fA-F]{64})", checksum_path.read_bytes())
    if match is None:
        raise ValueError(f"invalid checksum file: {checksum_path}")
    expected = match.group(1).decode().lower()
    if not path.exists() or sha256_file(path) != expected:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(get(url))
        actual = sha256_file(temporary)
        if actual != expected:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"checksum mismatch {url}: {actual} != {expected}")
        temporary.replace(path)
    return path


def bool_array(series: pd.Series) -> np.ndarray:
    if series.dtype == bool:
        return series.to_numpy(bool)
    return (
        series.astype(str)
        .str.strip()
        .str.lower()
        .isin(["true", "1", "t"])
        .to_numpy(bool)
    )


def iter_trades(
    path: Path,
    chunksize: int = 750_000,
) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"unexpected members in {path}: {members}")
        with archive.open(members[0]) as raw:
            first = raw.readline()
            raw.seek(0)
            token = first.split(b",", 1)[0].strip().lstrip(b"\xef\xbb\xbf")
            header = None if token.isdigit() else 0
            reader = pd.read_csv(
                raw,
                header=header,
                names=None if header == 0 else COLS,
                chunksize=chunksize,
                low_memory=False,
            )
            for frame in reader:
                if header == 0:
                    if frame.shape[1] < 7:
                        raise ValueError(f"unexpected schema in {path}: {frame.columns}")
                    frame = frame.iloc[:, :7]
                    frame.columns = COLS
                timestamps = pd.to_numeric(frame.transact_time, errors="coerce").to_numpy(float)
                prices = pd.to_numeric(frame.price, errors="coerce").to_numpy(float)
                quantities = pd.to_numeric(frame.quantity, errors="coerce").to_numpy(float)
                buyer_maker = bool_array(frame.is_buyer_maker)
                valid = (
                    np.isfinite(timestamps)
                    & np.isfinite(prices)
                    & np.isfinite(quantities)
                    & (prices > 0)
                    & (quantities > 0)
                )
                if not valid.any():
                    continue
                timestamps = timestamps[valid].astype("int64")
                prices = prices[valid]
                quantities = quantities[valid]
                buyer_maker = buyer_maker[valid]
                if np.median(timestamps) > 10**14:
                    timestamps //= 1000
                yield timestamps, prices, quantities, buyer_maker


def month_quote_total(path: Path) -> float:
    total = 0.0
    for _, prices, quantities, _ in iter_trades(path):
        total += float(np.dot(prices, quantities))
    return total


@dataclass
class BarState:
    threshold: float
    start_ms: int | None = None
    end_ms: int | None = None
    first_price: float = math.nan
    last_price: float = math.nan
    high: float = -math.inf
    low: float = math.inf
    quote: float = 0.0
    signed_quote: float = 0.0
    buy_quote: float = 0.0
    sell_quote: float = 0.0
    trades: int = 0

    def add(self, timestamp: int, price: float, quote: float, buyer_maker: bool) -> bool:
        if self.start_ms is None:
            self.start_ms = timestamp
            self.first_price = price
        self.end_ms = timestamp
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.quote += quote
        sign = -1.0 if buyer_maker else 1.0
        self.signed_quote += sign * quote
        if sign > 0:
            self.buy_quote += quote
        else:
            self.sell_quote += quote
        self.trades += 1
        return self.quote >= self.threshold

    def record(self, bar_id: int, month: str) -> dict[str, object]:
        return {
            "bar_id": bar_id,
            "month": month,
            "start_ms": self.start_ms,
            "signal_end_ms": self.end_ms,
            "first_price": self.first_price,
            "last_price": self.last_price,
            "high": self.high,
            "low": self.low,
            "total_quote": self.quote,
            "signed_quote": self.signed_quote,
            "buy_quote": self.buy_quote,
            "sell_quote": self.sell_quote,
            "trade_count": self.trades,
            "threshold_quote": self.threshold,
            "ofi": self.signed_quote / self.quote if self.quote > 0 else np.nan,
            "bar_return": math.log(self.last_price / self.first_price),
            "duration_seconds": (self.end_ms - self.start_ms) / 1000.0,
        }


class Builder:
    def __init__(self, target_year: int) -> None:
        self.target_year = target_year
        self.records: list[dict[str, object]] = []
        self.pending_entry: dict[str, object] | None = None
        self.active: list[dict[str, object]] = []
        self.bar_id = 0

    def process_month(self, path: Path, year: int, month: int, threshold: float) -> float:
        month_key = f"{year:04d}-{month:02d}"
        state = BarState(threshold)
        total = 0.0
        for timestamps, prices, quantities, buyer_maker in iter_trades(path):
            for timestamp, price, quantity, maker in zip(
                timestamps,
                prices,
                quantities,
                buyer_maker,
            ):
                timestamp = int(timestamp)
                price = float(price)
                quote = price * float(quantity)
                total += quote
                if (
                    self.pending_entry is not None
                    and timestamp > int(self.pending_entry["signal_end_ms"])
                ):
                    record = self.pending_entry
                    record["entry_ms"] = timestamp
                    record["entry_price"] = price
                    for horizon in HORIZONS_MIN:
                        record[f"target_ms_{horizon}"] = timestamp + horizon * 60_000
                        record[f"exit_ms_{horizon}"] = None
                        record[f"exit_price_{horizon}"] = None
                    self.active.append(record)
                    self.pending_entry = None
                remaining: list[dict[str, object]] = []
                for record in self.active:
                    for horizon in HORIZONS_MIN:
                        if (
                            record[f"exit_ms_{horizon}"] is None
                            and timestamp >= int(record[f"target_ms_{horizon}"])
                        ):
                            record[f"exit_ms_{horizon}"] = timestamp
                            record[f"exit_price_{horizon}"] = price
                    if all(
                        record[f"exit_ms_{horizon}"] is not None
                        for horizon in HORIZONS_MIN
                    ):
                        self.records.append(record)
                    else:
                        remaining.append(record)
                self.active = remaining
                if state.add(timestamp, price, quote, bool(maker)):
                    record = state.record(self.bar_id, month_key)
                    self.bar_id += 1
                    if pd.to_datetime(timestamp, unit="ms", utc=True).year == self.target_year:
                        self.pending_entry = record
                    state = BarState(threshold)
        return total


def build(
    symbol: str,
    year: int,
    cache: Path,
    output: Path,
    target_bars_per_day: int = 48,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    previous_year, previous_month = month_add(year, 1, -1)
    previous = ensure_archive(cache, symbol, previous_year, previous_month)
    manifest.append(
        {
            "year": previous_year,
            "month": previous_month,
            "path": str(previous),
            "sha256": sha256_file(previous),
        }
    )
    previous_total = month_quote_total(previous)
    builder = Builder(year)
    for month in range(1, 13):
        path = ensure_archive(cache, symbol, year, month)
        manifest.append(
            {
                "year": year,
                "month": month,
                "path": str(path),
                "sha256": sha256_file(path),
            }
        )
        previous_days = calendar.monthrange(previous_year, previous_month)[1]
        threshold = previous_total / (previous_days * target_bars_per_day)
        current_total = builder.process_month(path, year, month, threshold)
        previous_total = current_total
        previous_year, previous_month = year, month
        print(
            symbol,
            year,
            month,
            "threshold",
            threshold,
            "completed",
            len(builder.records),
            flush=True,
        )
    next_path = ensure_archive(cache, symbol, year + 1, 1)
    manifest.append(
        {
            "year": year + 1,
            "month": 1,
            "path": str(next_path),
            "sha256": sha256_file(next_path),
        }
    )
    threshold = previous_total / (calendar.monthrange(year, 12)[1] * target_bars_per_day)
    builder.process_month(next_path, year + 1, 1, threshold)
    frame = pd.DataFrame(builder.records)
    if frame.empty:
        raise RuntimeError("no completed bars")
    frame["symbol"] = symbol
    millisecond_columns = ["start_ms", "signal_end_ms", "entry_ms"] + [
        f"exit_ms_{horizon}" for horizon in HORIZONS_MIN
    ]
    for column in millisecond_columns:
        frame[column.replace("_ms", "_time")] = pd.to_datetime(
            frame[column],
            unit="ms",
            utc=True,
        )
    for horizon in HORIZONS_MIN:
        frame[f"y_{horizon}"] = np.log(
            frame[f"exit_price_{horizon}"] / frame.entry_price
        )
    frame = frame.sort_values("signal_end_ms").reset_index(drop=True)
    output_path = output / f"{symbol}_dollar_bars_{year}.csv.gz"
    frame.to_csv(
        output_path,
        index=False,
        compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
    )
    metadata = {
        "symbol": symbol,
        "year": year,
        "target_bars_per_day": target_bars_per_day,
        "bars": len(frame),
        "first_signal": str(frame.signal_end_time.min()),
        "last_signal": str(frame.signal_end_time.max()),
        "output": output_path.name,
        "output_sha256": sha256_file(output_path),
        "input_manifest": manifest,
    }
    (output / f"{symbol}_{year}_manifest.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-bars-per-day", type=int, default=48)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.symbol,
                args.year,
                args.cache,
                args.output,
                args.target_bars_per_day,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
