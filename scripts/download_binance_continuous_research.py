from __future__ import annotations

import argparse
import concurrent.futures
import io
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
)
START = pd.Timestamp("2022-01-01", tz="UTC")
END = pd.Timestamp("2026-07-01", tz="UTC")
ROOT = "https://data.binance.vision/data/futures/um"
COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)


@dataclass(frozen=True, slots=True)
class MonthTask:
    symbol: str
    month: pd.Timestamp


@dataclass(frozen=True, slots=True)
class MonthResult:
    symbol: str
    month: pd.Timestamp
    frame: pd.DataFrame
    monthly_url: str
    repaired_days: tuple[str, ...]


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def _download(url: str, *, retries: int) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "SMC-trading-system-continuous-research/1.0"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    assert last is not None
    raise RuntimeError(f"failed to download {url}: {last}") from last


def _timestamp_unit(values: pd.Series) -> str:
    maximum = int(pd.to_numeric(values, errors="raise").max())
    if maximum >= 10**17:
        return "ns"
    if maximum >= 10**14:
        return "us"
    if maximum >= 10**11:
        return "ms"
    return "s"


def _read_archive(payload: bytes, *, archive_name: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError(f"empty archive: {archive_name}")
        with archive.open(names[0]) as stream:
            raw = pd.read_csv(stream, header=None)
    if raw.empty:
        raise ValueError(f"empty CSV: {archive_name}")
    if not str(raw.iloc[0, 0]).strip().lstrip("-").isdigit():
        raw = raw.iloc[1:].reset_index(drop=True)
    if raw.shape[1] < 6:
        raise ValueError(f"unexpected kline shape {raw.shape}: {archive_name}")
    raw = raw.iloc[:, : min(raw.shape[1], len(COLUMNS))].copy()
    raw.columns = list(COLUMNS[: raw.shape[1]])
    unit = _timestamp_unit(raw["open_time"])
    frame = pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                pd.to_numeric(raw["open_time"], errors="raise"),
                unit=unit,
                utc=True,
            ),
            "open": pd.to_numeric(raw["open"], errors="raise"),
            "high": pd.to_numeric(raw["high"], errors="raise"),
            "low": pd.to_numeric(raw["low"], errors="raise"),
            "close": pd.to_numeric(raw["close"], errors="raise"),
            "volume": pd.to_numeric(raw["volume"], errors="raise"),
        }
    )
    return frame.drop_duplicates(subset=["open_time"], keep="last").sort_values(
        "open_time"
    )


def _month_bounds(month: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = max(START, month)
    end = min(END, month + pd.offsets.MonthBegin(1))
    return start, end


def _daily_frame(
    symbol: str,
    day: pd.Timestamp,
    *,
    retries: int,
) -> tuple[pd.DataFrame, str]:
    date_text = day.strftime("%Y-%m-%d")
    archive_name = f"{symbol}-5m-{date_text}.zip"
    url = f"{ROOT}/daily/klines/{symbol}/5m/{archive_name}"
    frame = _read_archive(
        _download(url, retries=retries),
        archive_name=archive_name,
    )
    frame = frame.loc[
        (frame["open_time"] >= day)
        & (frame["open_time"] < day + pd.Timedelta(days=1))
    ].copy()
    if len(frame) != 288:
        raise AssertionError(
            f"daily repair {symbol} {date_text}: expected 288 bars, got {len(frame)}"
        )
    return frame, url


def _fetch_month(task: MonthTask, *, retries: int) -> MonthResult:
    start, end = _month_bounds(task.month)
    month_text = task.month.strftime("%Y-%m")
    archive_name = f"{task.symbol}-5m-{month_text}.zip"
    monthly_url = (
        f"{ROOT}/monthly/klines/{task.symbol}/5m/{archive_name}"
    )
    frame = _read_archive(
        _download(monthly_url, retries=retries),
        archive_name=archive_name,
    )
    frame = frame.loc[
        (frame["open_time"] >= start) & (frame["open_time"] < end)
    ].copy()

    expected_days = pd.date_range(
        start.normalize(),
        end - pd.Timedelta(days=1),
        freq="1D",
    )
    counts = frame.groupby(frame["open_time"].dt.normalize()).size()
    repaired: list[str] = []
    daily_replacements: list[pd.DataFrame] = []
    bad_days: list[pd.Timestamp] = []
    for day in expected_days:
        if int(counts.get(day, 0)) != 288:
            bad_days.append(day)
    if bad_days:
        frame = frame.loc[
            ~frame["open_time"].dt.normalize().isin(bad_days)
        ].copy()
        for day in bad_days:
            replacement, _ = _daily_frame(
                task.symbol,
                day,
                retries=retries,
            )
            daily_replacements.append(replacement)
            repaired.append(day.strftime("%Y-%m-%d"))
    if daily_replacements:
        frame = pd.concat([frame, *daily_replacements], ignore_index=True)

    frame = frame.drop_duplicates(subset=["open_time"], keep="last").sort_values(
        "open_time"
    )
    expected = len(expected_days) * 288
    if len(frame) != expected:
        raise AssertionError(
            f"{task.symbol} {month_text}: expected {expected} bars, got {len(frame)}"
        )
    expected_index = pd.date_range(
        start,
        end - pd.Timedelta(minutes=5),
        freq="5min",
    )
    actual_index = pd.DatetimeIndex(frame["open_time"])
    missing = expected_index.difference(actual_index)
    unexpected = actual_index.difference(expected_index)
    if len(missing) or len(unexpected):
        raise AssertionError(
            f"{task.symbol} {month_text}: missing={len(missing)}, "
            f"unexpected={len(unexpected)}"
        )
    return MonthResult(
        symbol=task.symbol,
        month=task.month,
        frame=frame,
        monthly_url=monthly_url,
        repaired_days=tuple(repaired),
    )


def main() -> int:
    args = _args()
    if args.workers <= 0:
        raise SystemExit("workers must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    months = tuple(pd.date_range(START, END - pd.Timedelta(days=1), freq="MS"))
    tasks = [MonthTask(symbol, month) for symbol in SYMBOLS for month in months]
    by_symbol: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in SYMBOLS}
    manifest: list[dict[str, object]] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = {
            executor.submit(_fetch_month, task, retries=args.retries): task
            for task in tasks
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            by_symbol[result.symbol].append(result.frame)
            completed += 1
            print(
                f"[{completed}/{len(tasks)}] {result.symbol} "
                f"{result.month.strftime('%Y-%m')} "
                f"repairs={len(result.repaired_days)}",
                flush=True,
            )
            manifest.append(
                {
                    "symbol": result.symbol,
                    "month": result.month.strftime("%Y-%m"),
                    "bars": len(result.frame),
                    "monthly_url": result.monthly_url,
                    "repaired_days": "|".join(result.repaired_days),
                }
            )

    expected_index = pd.date_range(
        START,
        END - pd.Timedelta(minutes=5),
        freq="5min",
    )
    summary: list[dict[str, object]] = []
    for symbol, pieces in by_symbol.items():
        output = (
            pd.concat(pieces, ignore_index=True)
            .drop_duplicates(subset=["open_time"], keep="last")
            .sort_values("open_time")
        )
        actual_index = pd.DatetimeIndex(output["open_time"])
        missing = expected_index.difference(actual_index)
        unexpected = actual_index.difference(expected_index)
        if len(missing) or len(unexpected):
            raise AssertionError(
                f"{symbol}: missing={len(missing)}, unexpected={len(unexpected)}"
            )
        output.to_csv(args.output_dir / f"{symbol}_5m.csv", index=False)
        summary.append(
            {
                "symbol": symbol,
                "start": START.isoformat(),
                "end_exclusive": END.isoformat(),
                "bars": len(output),
                "calendar_days": int((END - START) / pd.Timedelta(days=1)),
            }
        )
        print(f"wrote {symbol}: {len(output)} bars", flush=True)

    pd.DataFrame(manifest).sort_values(["symbol", "month"]).to_csv(
        args.output_dir / "download_manifest.csv",
        index=False,
    )
    pd.DataFrame(summary).to_csv(
        args.output_dir / "universe_summary.csv",
        index=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
