from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

BASE_URL = "https://data.binance.vision/data/futures/um"
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]


@dataclass(frozen=True)
class DownloadSpec:
    dataset: str
    symbol: str
    interval: str
    period: str
    label: str
    url: str


@dataclass(frozen=True)
class PartitionRecord:
    dataset: str
    symbol: str
    interval: str
    period: str
    label: str
    url: str
    checksum_url: str
    checksum: str
    compressed_bytes: int
    member: str
    rows: int
    start: str
    end: str
    duplicate_timestamps: int
    internal_gap_count: int
    internal_missing_bars: int
    invalid_ohlc_rows: int
    parquet_path: str
    parquet_bytes: int


def month_labels(start_year: int, start_month: int, end_year: int, end_month: int) -> list[str]:
    labels: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        labels.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return labels


def day_labels(start: date, end: date) -> list[str]:
    labels: list[str] = []
    current = start
    while current <= end:
        labels.append(current.isoformat())
        current += timedelta(days=1)
    return labels


def make_specs(symbols: list[str], interval: str, daily_end: date) -> list[DownloadSpec]:
    specs: list[DownloadSpec] = []
    for symbol in symbols:
        for label in month_labels(2021, 1, 2026, 6):
            for dataset in ("klines", "markPriceKlines"):
                filename = f"{symbol}-{interval}-{label}.zip"
                url = f"{BASE_URL}/monthly/{dataset}/{symbol}/{interval}/{filename}"
                specs.append(DownloadSpec(dataset, symbol, interval, "monthly", label, url))
        for label in day_labels(date(2026, 7, 1), daily_end):
            for dataset in ("klines", "markPriceKlines"):
                filename = f"{symbol}-{interval}-{label}.zip"
                url = f"{BASE_URL}/daily/{dataset}/{symbol}/{interval}/{filename}"
                specs.append(DownloadSpec(dataset, symbol, interval, "daily", label, url))
    return specs


def request_bytes(url: str, *, attempts: int = 6, timeout: int = 120) -> bytes:
    headers = {"User-Agent": "smc-trading-system-research/1.0"}
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(min(30.0, 1.5 * (2**attempt)))
    raise RuntimeError(f"download failed after {attempts} attempts: {url}: {error}")


def parse_checksum(payload: bytes, url: str) -> str:
    text = payload.decode("utf-8", errors="strict").strip()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if match is None:
        raise ValueError(f"invalid checksum file: {url}: {text[:200]!r}")
    return match.group(1).lower()


def read_zip_csv(payload: bytes, spec: DownloadSpec) -> tuple[pd.DataFrame, str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected one CSV member in {spec.url}, found {members}")
        member = members[0]
        raw = archive.read(member)

    frame = pd.read_csv(io.BytesIO(raw), header=None, low_memory=False)
    first_value = pd.to_numeric(frame.iloc[0, 0], errors="coerce")
    if pd.isna(first_value):
        frame = frame.iloc[1:].reset_index(drop=True)
    if frame.shape[1] < len(KLINE_COLUMNS):
        raise ValueError(f"unexpected kline schema in {spec.url}: {frame.shape[1]} columns")
    frame = frame.iloc[:, : len(KLINE_COLUMNS)]
    frame.columns = KLINE_COLUMNS
    for name in KLINE_COLUMNS:
        frame[name] = pd.to_numeric(frame[name], errors="raise")

    raw_open = frame["open_time"].astype("int64")
    raw_close = frame["close_time"].astype("int64")
    magnitude = float(raw_open.abs().median())
    unit = "us" if magnitude >= 1e14 else "ms"
    frame["open_time"] = pd.to_datetime(raw_open, unit=unit, utc=True)
    frame["close_time"] = pd.to_datetime(raw_close, unit=unit, utc=True)
    frame["symbol"] = spec.symbol
    frame["source_period"] = spec.period
    frame["source_label"] = spec.label
    return frame, member


def validate_partition(frame: pd.DataFrame, interval_minutes: int) -> dict[str, object]:
    duplicate_timestamps = int(frame["open_time"].duplicated().sum())
    normalized = frame.sort_values("open_time").drop_duplicates("open_time", keep="last").reset_index(drop=True)
    invalid = (
        (normalized["high"] < normalized[["open", "close", "low"]].max(axis=1))
        | (normalized["low"] > normalized[["open", "close", "high"]].min(axis=1))
        | (normalized[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    expected = pd.Timedelta(minutes=interval_minutes)
    delta = normalized["open_time"].diff().dropna()
    gaps = delta[delta != expected]
    return {
        "frame": normalized,
        "rows": int(len(normalized)),
        "start": normalized["open_time"].iloc[0].isoformat(),
        "end": normalized["open_time"].iloc[-1].isoformat(),
        "duplicate_timestamps": duplicate_timestamps,
        "internal_gap_count": int(len(gaps)),
        "internal_missing_bars": int(sum(max(0, int(value / expected) - 1) for value in gaps)),
        "invalid_ohlc_rows": int(invalid.sum()),
    }


def partition_path(output: Path, spec: DownloadSpec) -> Path:
    year = spec.label[:4]
    month = spec.label[5:7]
    return output / spec.dataset / spec.symbol / f"year={year}" / f"month={month}" / f"{spec.label}.parquet"


def download_one(spec: DownloadSpec, output: Path, interval_minutes: int) -> PartitionRecord:
    checksum_url = spec.url + ".CHECKSUM"
    checksum = parse_checksum(request_bytes(checksum_url), checksum_url)
    payload = request_bytes(spec.url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != checksum:
        raise ValueError(f"checksum mismatch for {spec.url}: expected {checksum}, got {actual}")

    frame, member = read_zip_csv(payload, spec)
    audit = validate_partition(frame, interval_minutes)
    hard_failure = (
        audit["duplicate_timestamps"]
        or audit["invalid_ohlc_rows"]
        or (spec.dataset == "klines" and audit["internal_gap_count"])
    )
    if hard_failure:
        raise ValueError(f"partition integrity failure for {spec.url}: {audit}")
    if spec.dataset == "markPriceKlines" and audit["internal_gap_count"]:
        print(
            f"OUTAGE markPriceKlines {spec.symbol} {spec.label} "
            f"gaps={audit['internal_gap_count']} missing={audit['internal_missing_bars']}; "
            "the replay engine must prohibit entries and flatten safely across this interval",
            flush=True,
        )

    destination = partition_path(output, spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    audit["frame"].to_parquet(destination, index=False, compression="zstd")
    record = PartitionRecord(
        dataset=spec.dataset,
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        label=spec.label,
        url=spec.url,
        checksum_url=checksum_url,
        checksum=checksum,
        compressed_bytes=len(payload),
        member=member,
        rows=audit["rows"],
        start=audit["start"],
        end=audit["end"],
        duplicate_timestamps=audit["duplicate_timestamps"],
        internal_gap_count=audit["internal_gap_count"],
        internal_missing_bars=audit["internal_missing_bars"],
        invalid_ohlc_rows=audit["invalid_ohlc_rows"],
        parquet_path=str(destination),
        parquet_bytes=destination.stat().st_size,
    )
    print(
        f"OK {spec.dataset:16s} {spec.symbol} {spec.label} "
        f"rows={record.rows:7d} zip={record.compressed_bytes:10d} parquet={record.parquet_bytes:10d}",
        flush=True,
    )
    return record


def coverage(records: list[PartitionRecord], interval_minutes: int) -> list[dict[str, object]]:
    expected = pd.Timedelta(minutes=interval_minutes)
    rows: list[dict[str, object]] = []
    grouped: dict[tuple[str, str], list[PartitionRecord]] = {}
    for record in records:
        grouped.setdefault((record.dataset, record.symbol), []).append(record)
    for (dataset, symbol), items in sorted(grouped.items()):
        items.sort(key=lambda item: pd.Timestamp(item.start))
        boundary_gap_count = 0
        boundary_missing_bars = 0
        for left, right in zip(items, items[1:]):
            delta = pd.Timestamp(right.start) - pd.Timestamp(left.end)
            if delta != expected:
                boundary_gap_count += 1
                boundary_missing_bars += max(0, int(delta / expected) - 1)
        rows.append({
            "dataset": dataset,
            "symbol": symbol,
            "rows": int(sum(item.rows for item in items)),
            "start": items[0].start,
            "end": items[-1].end,
            "partitions": len(items),
            "internal_gap_count": int(sum(item.internal_gap_count for item in items)),
            "internal_missing_bars": int(sum(item.internal_missing_bars for item in items)),
            "boundary_gap_count": boundary_gap_count,
            "boundary_missing_bars": boundary_missing_bars,
            "duplicate_timestamps": int(sum(item.duplicate_timestamps for item in items)),
            "invalid_ohlc_rows": int(sum(item.invalid_ohlc_rows for item in items)),
            "replay_policy": (
                "fatal_on_any_gap"
                if dataset == "klines"
                else "explicit_outage_no_entry_no_interpolation"
            ),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("research_data/binance_official_1m"))
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--daily-end", type=date.fromisoformat, default=date(2026, 7, 20))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.interval != "1m":
        raise ValueError("this partitioned replay builder is intentionally fixed to 1m")
    args.output.mkdir(parents=True, exist_ok=True)

    specs = make_specs([symbol.upper() for symbol in args.symbols], args.interval, args.daily_end)
    records: list[PartitionRecord] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download_one, spec, args.output, 1): spec
            for spec in specs
        }
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                raise RuntimeError(f"required official archive failed: {spec}") from exc

    records.sort(key=lambda item: (item.dataset, item.symbol, item.start))
    manifest = {
        "source": "Binance Vision official USD-M public archive",
        "base_url": BASE_URL,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "interval": args.interval,
        "daily_end": args.daily_end.isoformat(),
        "partitioned": True,
        "replay_policy": {
            "trade_klines": "any duplicate, invalid OHLC, or missing minute is fatal",
            "mark_price": "gaps are recorded; no interpolation; no new entry and safe flattening across outage",
        },
        "archives": [asdict(record) for record in records],
        "coverage": coverage(records, 1),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    pd.DataFrame(manifest["coverage"]).to_csv(args.output / "coverage.csv", index=False)
    print(json.dumps(manifest["coverage"], indent=2), flush=True)


if __name__ == "__main__":
    main()
