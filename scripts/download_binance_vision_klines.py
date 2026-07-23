from __future__ import annotations

"""Download and validate Binance USDⓈ-M futures kline archives.

The script deliberately uses the exchange's public archive rather than the REST
API so a research snapshot can be reconstructed without API keys. Every ZIP is
verified against Binance's adjacent ``.CHECKSUM`` file before its rows are
accepted. Timestamps are normalized to UTC and no missing bars are fabricated.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import pandas as pd


ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
OUTPUT_COLUMNS = KLINE_COLUMNS[:-1]
_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
_CHECKSUM_PATTERN = re.compile(r"\b([0-9a-fA-F]{64})\b")


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    symbol: str
    interval: str
    month: str
    url: str
    checksum_url: str
    sha256: str
    bytes: int
    rows: int
    first_open_time: str
    last_open_time: str


@dataclass(frozen=True, slots=True)
class SeriesManifest:
    symbol: str
    interval: str
    rows: int
    first_open_time: str
    last_open_time: str
    duplicate_rows_removed: int
    gap_count: int
    missing_bar_count: int
    output_file: str
    output_sha256: str
    source_archives: tuple[ArchiveRecord, ...]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--interval", default="5m", choices=sorted(_INTERVAL_MS))
    parser.add_argument("--start-month", default="2020-12")
    parser.add_argument("--end-month", default="2026-06")
    parser.add_argument("--output-dir", type=Path, default=Path("data/s4_continuous_2020_2026"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/archive_cache"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def _month_range(start: str, end: str) -> tuple[str, ...]:
    begin = pd.Period(start, freq="M")
    finish = pd.Period(end, freq="M")
    if finish < begin:
        raise ValueError("end month must not precede start month")
    return tuple(str(period) for period in pd.period_range(begin, finish, freq="M"))


def _archive_url(symbol: str, interval: str, month: str) -> str:
    filename = f"{symbol}-{interval}-{month}.zip"
    return f"{ARCHIVE_ROOT}/{symbol}/{interval}/{filename}"


def _request_bytes(url: str, *, timeout: float, attempts: int = 5) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "smc-ict-research/1.0 (+reproducible-backtest)"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(min(2**attempt, 8))
    assert last_error is not None
    raise RuntimeError(f"failed to download {url}: {last_error}") from last_error


def _expected_sha256(checksum_payload: bytes, *, url: str) -> str:
    text = checksum_payload.decode("utf-8", errors="strict")
    match = _CHECKSUM_PATTERN.search(text)
    if match is None:
        raise ValueError(f"no SHA-256 digest found in {url}")
    return match.group(1).lower()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_epoch_to_ms(value: object) -> int:
    number = int(str(value).strip())
    magnitude = abs(number)
    if magnitude >= 10**17:
        return number // 1_000_000
    if magnitude >= 10**14:
        return number // 1_000
    if magnitude >= 10**11:
        return number
    if magnitude >= 10**8:
        return number * 1_000
    raise ValueError(f"unsupported epoch timestamp: {value!r}")


def _zip_rows(payload: bytes, *, source: str) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise ValueError(f"{source} must contain exactly one CSV, found {members}")
        with archive.open(members[0], "r") as binary:
            text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            rows = list(csv.reader(text))
    if not rows:
        raise ValueError(f"{source} contains no rows")
    if rows[0] and not rows[0][0].strip().lstrip("-").isdigit():
        rows = rows[1:]
    for index, row in enumerate(rows, start=1):
        if len(row) < len(KLINE_COLUMNS):
            raise ValueError(f"{source} row {index} has {len(row)} fields")
    return [row[: len(KLINE_COLUMNS)] for row in rows]


def _rows_to_frame(rows: Sequence[Sequence[str]], *, source: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    frame["open_time"] = frame["open_time"].map(_normalize_epoch_to_ms)
    frame["close_time"] = frame["close_time"].map(_normalize_epoch_to_ms)
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
        "taker_buy_quote_volume",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame["trade_count"] = pd.to_numeric(frame["trade_count"], errors="raise").astype("int64")
    frame = frame.loc[:, OUTPUT_COLUMNS]
    if frame.empty:
        raise ValueError(f"{source} contains no kline rows")
    return frame


def _validate_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
) -> tuple[pd.DataFrame, int, int, int]:
    before = len(frame)
    frame = frame.sort_values("open_time", kind="stable")
    frame = frame.drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)
    duplicates = before - len(frame)

    finite_columns = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
        "taker_buy_quote_volume",
    )
    for column in finite_columns:
        values = frame[column].astype(float)
        if not values.map(math.isfinite).all():
            raise ValueError(f"{symbol} {column} contains non-finite values")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"{symbol} contains non-positive prices")
    if (
        frame[["volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]]
        < 0
    ).any().any():
        raise ValueError(f"{symbol} contains negative volume")
    if not (
        (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    ).all():
        raise ValueError(f"{symbol} contains invalid OHLC ordering")

    expected = _INTERVAL_MS[interval]
    deltas = frame["open_time"].diff().dropna().astype("int64")
    if not deltas[deltas <= 0].empty:
        raise ValueError(f"{symbol} timestamps are not strictly increasing")
    misaligned = frame.loc[frame["open_time"] % expected != 0]
    if not misaligned.empty:
        raise ValueError(f"{symbol} contains {len(misaligned)} misaligned {interval} bars")
    gap_deltas = deltas[deltas > expected]
    non_multiple = gap_deltas[gap_deltas % expected != 0]
    if not non_multiple.empty:
        raise ValueError(f"{symbol} has gaps not divisible by the bar interval")
    gap_count = int(len(gap_deltas))
    missing_bar_count = int(((gap_deltas // expected) - 1).sum())
    return frame, duplicates, gap_count, missing_bar_count


def _write_csv_gzip(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.copy()
    output["open_time"] = pd.to_datetime(output["open_time"], unit="ms", utc=True).map(
        lambda value: value.isoformat()
    )
    output["close_time"] = pd.to_datetime(output["close_time"], unit="ms", utc=True).map(
        lambda value: value.isoformat()
    )
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                output.to_csv(text, index=False, lineterminator="\n")
    os.replace(temp, path)


def _download_archive(
    symbol: str,
    interval: str,
    month: str,
    *,
    cache_dir: Path,
    timeout: float,
) -> tuple[ArchiveRecord, pd.DataFrame]:
    url = _archive_url(symbol, interval, month)
    checksum_url = f"{url}.CHECKSUM"
    filename = url.rsplit("/", 1)[-1]
    cache_path = cache_dir / symbol / interval / filename
    expected = _expected_sha256(
        _request_bytes(checksum_url, timeout=timeout),
        url=checksum_url,
    )

    if cache_path.exists() and _sha256_file(cache_path) == expected:
        payload = cache_path.read_bytes()
    else:
        payload = _request_bytes(url, timeout=timeout)
        actual = _sha256_bytes(payload)
        if actual != expected:
            raise ValueError(
                f"SHA-256 mismatch for {url}: expected {expected}, got {actual}"
            )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temp.write_bytes(payload)
        os.replace(temp, cache_path)

    actual = _sha256_bytes(payload)
    if actual != expected:
        raise ValueError(
            f"cached SHA-256 mismatch for {url}: expected {expected}, got {actual}"
        )
    rows = _zip_rows(payload, source=url)
    frame = _rows_to_frame(rows, source=url)
    first = pd.to_datetime(int(frame["open_time"].min()), unit="ms", utc=True).isoformat()
    last = pd.to_datetime(int(frame["open_time"].max()), unit="ms", utc=True).isoformat()
    record = ArchiveRecord(
        symbol=symbol,
        interval=interval,
        month=month,
        url=url,
        checksum_url=checksum_url,
        sha256=actual,
        bytes=len(payload),
        rows=len(frame),
        first_open_time=first,
        last_open_time=last,
    )
    return record, frame


def _build_symbol(
    symbol: str,
    interval: str,
    months: Sequence[str],
    *,
    output_dir: Path,
    cache_dir: Path,
    workers: int,
    timeout: float,
) -> SeriesManifest:
    normalized_symbol = symbol.upper().strip()
    records: list[ArchiveRecord] = []
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                _download_archive,
                normalized_symbol,
                interval,
                month,
                cache_dir=cache_dir,
                timeout=timeout,
            ): month
            for month in months
        }
        for future in as_completed(futures):
            month = futures[future]
            record, frame = future.result()
            print(
                f"downloaded {normalized_symbol} {interval} {month}: {len(frame):,} rows",
                flush=True,
            )
            records.append(record)
            frames.append(frame)

    combined = pd.concat(frames, ignore_index=True)
    combined, duplicates, gap_count, missing_bar_count = _validate_frame(
        combined,
        symbol=normalized_symbol,
        interval=interval,
    )
    output_path = output_dir / f"{normalized_symbol}_{interval}.csv.gz"
    _write_csv_gzip(combined, output_path)
    first = pd.to_datetime(
        int(combined["open_time"].iloc[0]), unit="ms", utc=True
    ).isoformat()
    last = pd.to_datetime(
        int(combined["open_time"].iloc[-1]), unit="ms", utc=True
    ).isoformat()
    return SeriesManifest(
        symbol=normalized_symbol,
        interval=interval,
        rows=len(combined),
        first_open_time=first,
        last_open_time=last,
        duplicate_rows_removed=duplicates,
        gap_count=gap_count,
        missing_bar_count=missing_bar_count,
        output_file=output_path.name,
        output_sha256=_sha256_file(output_path),
        source_archives=tuple(sorted(records, key=lambda item: item.month)),
    )


def main() -> int:
    args = _parse_args()
    months = _month_range(args.start_month, args.end_month)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifests = [
        _build_symbol(
            symbol,
            args.interval,
            months,
            output_dir=args.output_dir,
            cache_dir=args.cache_dir,
            workers=args.workers,
            timeout=args.timeout,
        )
        for symbol in args.symbols
    ]
    payload = {
        "source": "Binance Vision USD-M monthly kline archives",
        "archive_root": ARCHIVE_ROOT,
        "start_month": args.start_month,
        "end_month": args.end_month,
        "created_by": "scripts/download_binance_vision_klines.py",
        "series": [asdict(item) for item in manifests],
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "series": [
                    asdict(item) | {"source_archives": len(item.source_archives)}
                    for item in manifests
                ],
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
