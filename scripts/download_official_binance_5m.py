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
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]


@dataclass(frozen=True)
class DownloadSpec:
    dataset: str
    symbol: str
    interval: str | None
    period: str
    label: str
    url: str


@dataclass(frozen=True)
class DownloadRecord:
    dataset: str
    symbol: str
    period: str
    label: str
    url: str
    checksum_url: str
    checksum: str
    bytes: int
    member: str
    rows: int


def parse_month(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if match is None:
        raise argparse.ArgumentTypeError("month must use YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("month must be in 01..12")
    return year, month


def month_labels(start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    if start > end:
        raise ValueError("monthly-start must not be after monthly-end")
    labels: list[str] = []
    year, month = start
    while (year, month) <= end:
        labels.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return labels


def day_labels(start: date, end: date) -> list[str]:
    if start > end:
        return []
    labels: list[str] = []
    current = start
    while current <= end:
        labels.append(current.isoformat())
        current += timedelta(days=1)
    return labels


def make_specs(
    symbols: list[str],
    interval: str,
    *,
    monthly_start: tuple[int, int],
    monthly_end: tuple[int, int],
    daily_start: date | None,
    daily_end: date | None,
    datasets: tuple[str, ...],
    include_funding: bool,
) -> list[DownloadSpec]:
    allowed = {"klines", "markPriceKlines"}
    unknown = set(datasets) - allowed
    if unknown:
        raise ValueError(f"unsupported datasets: {sorted(unknown)}")

    specs: list[DownloadSpec] = []
    for symbol in symbols:
        for label in month_labels(monthly_start, monthly_end):
            for dataset in datasets:
                filename = f"{symbol}-{interval}-{label}.zip"
                url = f"{BASE_URL}/monthly/{dataset}/{symbol}/{interval}/{filename}"
                specs.append(DownloadSpec(dataset, symbol, interval, "monthly", label, url))
            if include_funding:
                filename = f"{symbol}-fundingRate-{label}.zip"
                url = f"{BASE_URL}/monthly/fundingRate/{symbol}/{filename}"
                specs.append(DownloadSpec("fundingRate", symbol, None, "monthly", label, url))

        if daily_start is not None and daily_end is not None:
            for label in day_labels(daily_start, daily_end):
                for dataset in datasets:
                    filename = f"{symbol}-{interval}-{label}.zip"
                    url = f"{BASE_URL}/daily/{dataset}/{symbol}/{interval}/{filename}"
                    specs.append(DownloadSpec(dataset, symbol, interval, "daily", label, url))
    return specs


def request_bytes(url: str, *, attempts: int = 5, timeout: int = 90) -> bytes:
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
            time.sleep(min(20.0, 1.5 * (2**attempt)))
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

    if spec.dataset in {"klines", "markPriceKlines"}:
        frame = pd.read_csv(io.BytesIO(raw), header=None, low_memory=False)
        first = pd.to_numeric(frame.iloc[0, 0], errors="coerce")
        if pd.isna(first):
            frame = frame.iloc[1:].reset_index(drop=True)
        if frame.shape[1] < len(KLINE_COLUMNS):
            raise ValueError(f"unexpected kline schema in {spec.url}: {frame.shape[1]} columns")
        frame = frame.iloc[:, : len(KLINE_COLUMNS)]
        frame.columns = KLINE_COLUMNS
        numeric = [name for name in KLINE_COLUMNS if name not in {"open_time", "close_time"}]
        for name in ("open_time", "close_time", *numeric):
            frame[name] = pd.to_numeric(frame[name], errors="raise")
        raw_open = frame["open_time"].astype("int64")
        raw_close = frame["close_time"].astype("int64")
        unit = "us" if float(raw_open.abs().median()) >= 1e14 else "ms"
        frame["open_time"] = pd.to_datetime(raw_open, unit=unit, utc=True)
        frame["close_time"] = pd.to_datetime(raw_close, unit=unit, utc=True)
        frame["symbol"] = spec.symbol
        frame["source_period"] = spec.period
        frame["source_label"] = spec.label
        return frame, member

    frame = pd.read_csv(io.BytesIO(raw), low_memory=False)
    if frame.empty:
        raise ValueError(f"empty funding archive: {spec.url}")
    if all(str(name).isdigit() for name in frame.columns):
        frame = pd.read_csv(io.BytesIO(raw), header=None, low_memory=False)
    columns = [str(name) for name in frame.columns]
    time_name = next(
        (name for name in columns if name.lower() in {"calc_time", "fundingtime", "funding_time", "time", "timestamp"}),
        columns[0],
    )
    rate_name = next((name for name in columns if "rate" in name.lower()), columns[-1])
    time_values = pd.to_numeric(frame[time_name], errors="coerce")
    if time_values.notna().mean() < 0.95:
        parsed = pd.to_datetime(frame[time_name], utc=True, errors="coerce")
    else:
        magnitude = float(time_values.dropna().abs().median())
        unit = "us" if magnitude >= 1e14 else "ms"
        parsed = pd.to_datetime(time_values.astype("Int64"), unit=unit, utc=True, errors="coerce")
    rate = pd.to_numeric(frame[rate_name], errors="coerce")
    output = pd.DataFrame(
        {
            "funding_time": parsed,
            "funding_rate": rate,
            "symbol": spec.symbol,
            "source_period": spec.period,
            "source_label": spec.label,
        }
    ).dropna(subset=["funding_time", "funding_rate"])
    if output.empty:
        raise ValueError(f"could not parse funding archive: {spec.url}; columns={columns}")
    return output, member


def download_one(spec: DownloadSpec) -> tuple[DownloadRecord, pd.DataFrame]:
    checksum_url = spec.url + ".CHECKSUM"
    checksum = parse_checksum(request_bytes(checksum_url), checksum_url)
    payload = request_bytes(spec.url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != checksum:
        raise ValueError(f"checksum mismatch for {spec.url}: expected {checksum}, got {actual}")
    frame, member = read_zip_csv(payload, spec)
    record = DownloadRecord(
        dataset=spec.dataset,
        symbol=spec.symbol,
        period=spec.period,
        label=spec.label,
        url=spec.url,
        checksum_url=checksum_url,
        checksum=checksum,
        bytes=len(payload),
        member=member,
        rows=len(frame),
    )
    print(
        f"OK {spec.dataset:16s} {spec.symbol} {spec.label} rows={len(frame):7d} bytes={len(payload):10d}",
        flush=True,
    )
    return record, frame


def validate_klines(frame: pd.DataFrame, interval_minutes: int) -> dict[str, object]:
    duplicates = int(frame["open_time"].duplicated().sum())
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last").reset_index(drop=True)
    if frame.empty:
        raise ValueError("empty normalized kline frame")
    invalid = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    if invalid.any():
        raise ValueError(f"invalid OHLC rows: {int(invalid.sum())}")
    delta = frame["open_time"].diff().dropna()
    expected = pd.Timedelta(minutes=interval_minutes)
    gaps = delta[delta != expected]
    return {
        "rows": int(len(frame)),
        "start": frame["open_time"].iloc[0].isoformat(),
        "end": frame["open_time"].iloc[-1].isoformat(),
        "duplicate_timestamps": duplicates,
        "gap_count": int(len(gaps)),
        "missing_bars": int(sum(max(0, int(value / expected) - 1) for value in gaps)),
        "invalid_ohlc_rows": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("research_data/binance_official_5m"))
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--monthly-start", type=parse_month, default=parse_month("2021-01"))
    parser.add_argument("--monthly-end", type=parse_month, default=parse_month("2026-06"))
    parser.add_argument("--daily-start", type=date.fromisoformat, default=date(2026, 7, 1))
    parser.add_argument("--daily-end", type=date.fromisoformat, default=date(2026, 7, 20))
    parser.add_argument("--no-daily", action="store_true")
    parser.add_argument("--datasets", nargs="+", default=["klines", "markPriceKlines"])
    parser.add_argument("--no-funding", action="store_true")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    daily_start = None if args.no_daily else args.daily_start
    daily_end = None if args.no_daily else args.daily_end
    specs = make_specs(
        [symbol.upper() for symbol in args.symbols],
        args.interval,
        monthly_start=args.monthly_start,
        monthly_end=args.monthly_end,
        daily_start=daily_start,
        daily_end=daily_end,
        datasets=tuple(args.datasets),
        include_funding=not args.no_funding,
    )
    records: list[DownloadRecord] = []
    frames: dict[tuple[str, str], list[pd.DataFrame]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, spec): spec for spec in specs}
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            try:
                record, frame = future.result()
            except Exception as exc:
                raise RuntimeError(f"required official archive failed: {spec}") from exc
            records.append(record)
            frames.setdefault((spec.dataset, spec.symbol), []).append(frame)

    manifest: dict[str, object] = {
        "source": "Binance Vision official USD-M public archive",
        "base_url": BASE_URL,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "interval": args.interval,
        "monthly_start": f"{args.monthly_start[0]:04d}-{args.monthly_start[1]:02d}",
        "monthly_end": f"{args.monthly_end[0]:04d}-{args.monthly_end[1]:02d}",
        "daily_start": None if daily_start is None else daily_start.isoformat(),
        "daily_end": None if daily_end is None else daily_end.isoformat(),
        "archives": [asdict(record) for record in sorted(records, key=lambda item: (item.dataset, item.symbol, item.label))],
        "datasets": {},
    }

    interval_minutes = int(args.interval.removesuffix("m"))
    for (dataset, symbol), parts in sorted(frames.items()):
        frame = pd.concat(parts, ignore_index=True)
        time_column = "funding_time" if dataset == "fundingRate" else "open_time"
        frame = frame.sort_values(time_column).drop_duplicates(time_column, keep="last").reset_index(drop=True)
        suffix = args.interval if dataset != "fundingRate" else "8h"
        path = args.output / f"{symbol}_{dataset}_{suffix}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        if dataset == "fundingRate":
            audit = {
                "rows": int(len(frame)),
                "start": frame[time_column].iloc[0].isoformat(),
                "end": frame[time_column].iloc[-1].isoformat(),
                "duplicate_timestamps": 0,
            }
        else:
            audit = validate_klines(frame, interval_minutes=interval_minutes)
        audit["path"] = str(path)
        audit["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["datasets"][f"{symbol}:{dataset}"] = audit
        print(f"WROTE {path} {audit}", flush=True)

    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"WROTE {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
