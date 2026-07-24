from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import io
from pathlib import Path
import time
from typing import Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

import pandas as pd


BASE_URL = "https://data.binance.vision/data/futures/um"
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


@dataclass(frozen=True, slots=True, order=True)
class DateInterval:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("interval end must follow start")


def merge_intervals(intervals: Iterable[DateInterval]) -> tuple[DateInterval, ...]:
    ordered = sorted(intervals)
    if not ordered:
        raise ValueError("at least one interval is required")
    merged: list[DateInterval] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = DateInterval(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return tuple(merged)


def intervals_from_manifest(payload: dict[str, object]) -> tuple[DateInterval, ...]:
    if payload.get("schema") != "ictbt.random_annual_windows.v1":
        raise ValueError("unsupported random-window manifest schema")
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("manifest contains no samples")
    intervals: list[DateInterval] = []
    for raw_sample in raw_samples:
        if not isinstance(raw_sample, dict):
            raise ValueError("invalid sample record")
        raw_windows = raw_sample.get("windows")
        if not isinstance(raw_windows, list):
            raise ValueError("sample contains no windows")
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                raise ValueError("invalid window record")
            intervals.append(
                DateInterval(
                    date.fromisoformat(str(raw_window["warmup_start"])),
                    date.fromisoformat(str(raw_window["end"])),
                )
            )
    return merge_intervals(intervals)


def months_for_intervals(intervals: Sequence[DateInterval]) -> tuple[str, ...]:
    months: set[str] = set()
    for interval in intervals:
        cursor = date(interval.start.year, interval.start.month, 1)
        final = interval.end - timedelta(days=1)
        while cursor <= final:
            months.add(f"{cursor.year:04d}-{cursor.month:02d}")
            cursor = (
                date(cursor.year + 1, 1, 1)
                if cursor.month == 12
                else date(cursor.year, cursor.month + 1, 1)
            )
    return tuple(sorted(months))


def _download(url: str, *, attempts: int = 4) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "ictbt-research/1.0"})
            with urlopen(request, timeout=90) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 8))
    assert last_error is not None
    raise last_error


def _verify_checksum(payload: bytes, checksum_payload: bytes, *, name: str) -> None:
    expected = checksum_payload.decode("utf-8").strip().split()[0].lower()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {name}: {actual} != {expected}")


def _month_urls(symbol: str, interval: str, month: str) -> tuple[str, str]:
    name = f"{symbol}-{interval}-{month}.zip"
    base = f"{BASE_URL}/monthly/klines/{symbol}/{interval}/{name}"
    return base, f"{base}.CHECKSUM"


def _day_urls(symbol: str, interval: str, day: date) -> tuple[str, str]:
    name = f"{symbol}-{interval}-{day.isoformat()}.zip"
    base = f"{BASE_URL}/daily/klines/{symbol}/{interval}/{name}"
    return base, f"{base}.CHECKSUM"


def _read_zip(payload: bytes, *, source_name: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"{source_name} must contain exactly one CSV")
        with archive.open(members[0]) as source:
            raw = pd.read_csv(source, header=None, dtype=str)
    if raw.empty:
        raise ValueError(f"{source_name} contains no rows")
    if str(raw.iloc[0, 0]).strip().lower() in {"open_time", "open time"}:
        raw = raw.iloc[1:].reset_index(drop=True)
    if raw.shape[1] != len(KLINE_COLUMNS):
        raise ValueError(
            f"{source_name} has {raw.shape[1]} columns; expected {len(KLINE_COLUMNS)}"
        )
    raw.columns = KLINE_COLUMNS
    return raw


def normalize_open_times(values: pd.Series) -> pd.DatetimeIndex:
    numeric = pd.to_numeric(values, errors="raise").astype("int64")
    magnitude = int(numeric.abs().median())
    if magnitude >= 10**17:
        unit = "ns"
    elif magnitude >= 10**14:
        unit = "us"
    elif magnitude >= 10**11:
        unit = "ms"
    else:
        unit = "s"
    return pd.DatetimeIndex(pd.to_datetime(numeric, unit=unit, utc=True))


def _normalize_frame(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.loc[:, KLINE_COLUMNS].copy()
    frame.index = normalize_open_times(frame.pop("open_time"))
    frame.index.name = "open_time"
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    return frame.loc[:, ("open", "high", "low", "close", "volume")]


def _month_days(month: str) -> tuple[date, ...]:
    year, month_number = (int(part) for part in month.split("-"))
    start = date(year, month_number, 1)
    end = (
        date(year + 1, 1, 1)
        if month_number == 12
        else date(year, month_number + 1, 1)
    )
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days))


def _date_is_requested(day: date, intervals: Sequence[DateInterval]) -> bool:
    return any(interval.start <= day < interval.end for interval in intervals)


def _load_month_or_days(
    *,
    symbol: str,
    interval: str,
    month: str,
    intervals: Sequence[DateInterval],
    cache_dir: Path,
) -> pd.DataFrame:
    month_url, checksum_url = _month_urls(symbol, interval, month)
    cache_path = cache_dir / symbol / interval / Path(month_url).name
    checksum_path = cache_path.with_suffix(cache_path.suffix + ".CHECKSUM")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if cache_path.exists() and checksum_path.exists():
            payload = cache_path.read_bytes()
            checksum_payload = checksum_path.read_bytes()
        else:
            payload = _download(month_url)
            checksum_payload = _download(checksum_url)
            _verify_checksum(payload, checksum_payload, name=cache_path.name)
            cache_path.write_bytes(payload)
            checksum_path.write_bytes(checksum_payload)
        _verify_checksum(payload, checksum_payload, name=cache_path.name)
        return _normalize_frame(_read_zip(payload, source_name=cache_path.name))
    except HTTPError as exc:
        if exc.code != 404:
            raise

    daily: list[pd.DataFrame] = []
    for day in _month_days(month):
        if not _date_is_requested(day, intervals):
            continue
        day_url, day_checksum_url = _day_urls(symbol, interval, day)
        day_cache = cache_dir / symbol / interval / "daily" / Path(day_url).name
        day_checksum = day_cache.with_suffix(day_cache.suffix + ".CHECKSUM")
        day_cache.parent.mkdir(parents=True, exist_ok=True)
        if day_cache.exists() and day_checksum.exists():
            payload = day_cache.read_bytes()
            checksum_payload = day_checksum.read_bytes()
        else:
            payload = _download(day_url)
            checksum_payload = _download(day_checksum_url)
            _verify_checksum(payload, checksum_payload, name=day_cache.name)
            day_cache.write_bytes(payload)
            day_checksum.write_bytes(checksum_payload)
        _verify_checksum(payload, checksum_payload, name=day_cache.name)
        daily.append(_normalize_frame(_read_zip(payload, source_name=day_cache.name)))
    if not daily:
        raise FileNotFoundError(f"no monthly or daily data found for {symbol} {month}")
    return pd.concat(daily).sort_index()


def _filter_and_validate(
    frame: pd.DataFrame,
    *,
    intervals: Sequence[DateInterval],
    interval: str,
) -> pd.DataFrame:
    if interval != "5m":
        raise ValueError("this research downloader currently locks the source to 5m")
    pieces: list[pd.DataFrame] = []
    for requested in intervals:
        start = pd.Timestamp(requested.start, tz="UTC")
        end = pd.Timestamp(requested.end, tz="UTC")
        piece = frame.loc[(frame.index >= start) & (frame.index < end)].copy()
        expected = pd.date_range(start, end, freq="5min", inclusive="left")
        if not piece.index.equals(expected):
            missing = expected.difference(piece.index)
            duplicates = int(piece.index.duplicated().sum())
            raise ValueError(
                f"non-contiguous Binance data in [{start}, {end}): "
                f"missing={len(missing)}, duplicates={duplicates}"
            )
        pieces.append(piece)
    output = pd.concat(pieces).sort_index()
    output = output.loc[~output.index.duplicated(keep="first")]
    return output


def download_symbol(
    *,
    symbol: str,
    interval: str,
    intervals: Sequence[DateInterval],
    cache_dir: Path,
    output_dir: Path,
) -> Path:
    frames = [
        _load_month_or_days(
            symbol=symbol,
            interval=interval,
            month=month,
            intervals=intervals,
            cache_dir=cache_dir,
        )
        for month in months_for_intervals(intervals)
    ]
    combined = pd.concat(frames).sort_index()
    combined = combined.loc[~combined.index.duplicated(keep="first")]
    selected = _filter_and_validate(combined, intervals=intervals, interval=interval)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{symbol}_{interval}.csv"
    selected.reset_index().to_csv(target, index=False)
    return target
