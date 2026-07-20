from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import shutil
import urllib.error
import urllib.request
import zipfile

import pandas as pd


BASE_URL = "https://data.binance.vision/data/futures/um"
INTERVAL = "5m"
KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
)


def _utc_day(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("date must be valid")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.normalize()


def _month_start(value: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(year=value.year, month=value.month, day=1, tz="UTC")


def _next_month(value: pd.Timestamp) -> pd.Timestamp:
    if value.month == 12:
        return pd.Timestamp(year=value.year + 1, month=1, day=1, tz="UTC")
    return pd.Timestamp(year=value.year, month=value.month + 1, day=1, tz="UTC")


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    symbol: str
    period: str
    label: str
    covered_start: pd.Timestamp
    covered_end: pd.Timestamp

    def __post_init__(self) -> None:
        if not self.symbol or self.period not in {"monthly", "daily"} or not self.label:
            raise ValueError("archive identity is invalid")
        start = _utc_day(self.covered_start)
        end = _utc_day(self.covered_end)
        if end <= start:
            raise ValueError("archive coverage must contain at least one day")
        object.__setattr__(self, "covered_start", start)
        object.__setattr__(self, "covered_end", end)

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{INTERVAL}-{self.label}.zip"

    @property
    def url(self) -> str:
        return (
            f"{BASE_URL}/{self.period}/klines/{self.symbol}/{INTERVAL}/"
            f"{self.filename}"
        )

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"


def plan_archives(
    symbol: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[ArchiveRequest, ...]:
    """Request one monthly archive per touched month.

    Historical boundary months are still cheapest as one monthly file.  When a
    current or otherwise unavailable monthly file returns 404, the request's
    exact covered date range is expanded into daily fallback files.
    """

    if not symbol:
        raise ValueError("symbol is required")
    begin = _utc_day(start)
    finish_requested = _utc_day(end)
    if not begin < finish_requested:
        raise ValueError("start must precede end")
    output: list[ArchiveRequest] = []
    cursor = _month_start(begin)
    while cursor < finish_requested:
        month_end = _next_month(cursor)
        output.append(
            ArchiveRequest(
                symbol=symbol,
                period="monthly",
                label=cursor.strftime("%Y-%m"),
                covered_start=max(begin, cursor),
                covered_end=min(finish_requested, month_end),
            )
        )
        cursor = month_end
    return tuple(output)


def _request_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SMC-trading-system/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download(
    request: ArchiveRequest,
    *,
    cache_dir: Path,
    verify_checksum: bool,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / request.filename
    if not target.exists():
        try:
            payload = _request_bytes(request.url)
        except urllib.error.HTTPError as exc:
            raise FileNotFoundError(request.url) from exc
        target.write_bytes(payload)
    if verify_checksum:
        checksum_text = _request_bytes(request.checksum_url).decode("utf-8").strip()
        expected = checksum_text.split()[0].lower()
        actual = hashlib.sha256(target.read_bytes()).hexdigest().lower()
        if actual != expected:
            target.unlink(missing_ok=True)
            raise ValueError(f"checksum mismatch for {request.filename}")
    return target


def _daily_fallback(monthly: ArchiveRequest) -> tuple[ArchiveRequest, ...]:
    if monthly.period != "monthly":
        raise ValueError("daily fallback requires a monthly request")
    return tuple(
        ArchiveRequest(
            symbol=monthly.symbol,
            period="daily",
            label=day.strftime("%Y-%m-%d"),
            covered_start=day,
            covered_end=day + pd.Timedelta(days=1),
        )
        for day in pd.date_range(
            monthly.covered_start,
            monthly.covered_end - pd.Timedelta(days=1),
            freq="1D",
        )
    )


def _archive_members(path: Path) -> tuple[bytes, ...]:
    with zipfile.ZipFile(path) as archive:
        names = tuple(
            name
            for name in archive.namelist()
            if not name.endswith("/") and name.lower().endswith(".csv")
        )
        if not names:
            raise ValueError(f"no CSV member in {path.name}")
        return tuple(archive.read(name) for name in names)


def _epoch_unit(values: pd.Series) -> str:
    maximum = int(pd.to_numeric(values, errors="raise").max())
    if maximum >= 10**17:
        return "ns"
    if maximum >= 10**14:
        return "us"
    if maximum >= 10**11:
        return "ms"
    return "s"


def _parse_member(payload: bytes) -> pd.DataFrame:
    raw = pd.read_csv(io.BytesIO(payload), header=None)
    if raw.empty:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    if str(raw.iloc[0, 0]).strip().lower() == "open_time":
        raw = raw.iloc[1:].reset_index(drop=True)
    if raw.shape[1] < len(KLINE_COLUMNS):
        raise ValueError("unexpected Binance kline column count")
    raw = raw.iloc[:, : len(KLINE_COLUMNS)]
    raw.columns = KLINE_COLUMNS
    unit = _epoch_unit(raw["open_time"])
    raw["open_time"] = pd.to_datetime(
        pd.to_numeric(raw["open_time"], errors="raise"),
        unit=unit,
        utc=True,
    )
    for column in ("open", "high", "low", "close", "volume"):
        raw[column] = pd.to_numeric(raw[column], errors="raise").astype(float)
    return raw.loc[:, ("open_time", "open", "high", "low", "close", "volume")]


def _load_archive(path: Path) -> pd.DataFrame:
    frames = [_parse_member(payload) for payload in _archive_members(path)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _download_request_with_fallback(
    request: ArchiveRequest,
    *,
    cache_dir: Path,
    verify_checksum: bool,
) -> tuple[Path, ...]:
    try:
        return (
            _download(
                request,
                cache_dir=cache_dir,
                verify_checksum=verify_checksum,
            ),
        )
    except FileNotFoundError:
        if request.period != "monthly":
            raise
        output: list[Path] = []
        for daily in _daily_fallback(request):
            output.append(
                _download(
                    daily,
                    cache_dir=cache_dir,
                    verify_checksum=verify_checksum,
                )
            )
        return tuple(output)


def download_symbol(
    symbol: str,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    output_dir: Path,
    cache_dir: Path,
    verify_checksum: bool = True,
) -> Path:
    begin = _utc_day(start)
    finish = _utc_day(end)
    requests = plan_archives(symbol, start=begin, end=finish)
    archives: list[Path] = []
    for ordinal, request in enumerate(requests, start=1):
        print(
            f"[{symbol} {ordinal}/{len(requests)}] {request.period} {request.label}",
            flush=True,
        )
        archives.extend(
            _download_request_with_fallback(
                request,
                cache_dir=cache_dir / symbol,
                verify_checksum=verify_checksum,
            )
        )

    frames = [_load_archive(path) for path in archives]
    if not frames:
        raise ValueError(f"no archives downloaded for {symbol}")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.loc[
        (combined["open_time"] >= begin) & (combined["open_time"] < finish)
    ].copy()
    combined = combined.sort_values("open_time").drop_duplicates(
        subset=["open_time"],
        keep="last",
    )
    expected_index = pd.date_range(begin, finish, freq="5min", inclusive="left")
    actual_index = pd.DatetimeIndex(combined["open_time"])
    if not actual_index.equals(expected_index):
        missing = expected_index.difference(actual_index)
        extras = actual_index.difference(expected_index)
        raise ValueError(
            f"{symbol} is not a contiguous 5m series: "
            f"missing={len(missing)}, extras={len(extras)}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{symbol}_5m.csv"
    combined["open_time"] = combined["open_time"].map(
        lambda value: value.isoformat()
    )
    combined.to_csv(target, index=False, encoding="utf-8")
    return target


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download official Binance USD-M futures 5m public klines"
    )
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", default="2021-11-01")
    parser.add_argument("--end", required=True, help="exclusive UTC date")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/binance_um_5m"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/binance_public_data"),
    )
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    start = _utc_day(args.start)
    end = _utc_day(args.end)
    if not start < end:
        raise SystemExit("start must precede end")
    if args.clear_cache and args.cache_dir.exists():
        shutil.rmtree(args.cache_dir)
    for symbol in tuple(dict.fromkeys(str(item).upper() for item in args.symbols)):
        target = download_symbol(
            symbol,
            start=start,
            end=end,
            output_dir=args.output_dir,
            cache_dir=args.cache_dir,
            verify_checksum=not args.no_checksum,
        )
        print(target, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
