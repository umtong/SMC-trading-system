from __future__ import annotations

import argparse
import concurrent.futures
import io
import time
import urllib.error
import urllib.request
import zipfile
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

WINDOWS = (
    ("2024-12-30", "2025-01-13"),
    ("2025-07-08", "2025-07-22"),
    ("2026-01-23", "2026-02-06"),
    ("2026-04-04", "2026-04-18"),
    ("2026-06-08", "2026-06-22"),
)

ROOT = "https://data.binance.vision/data/futures/um/daily/klines"
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


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def _dates() -> tuple[pd.Timestamp, ...]:
    values: set[pd.Timestamp] = set()
    for start, end in WINDOWS:
        values.update(
            pd.date_range(
                pd.Timestamp(start, tz="UTC"),
                pd.Timestamp(end, tz="UTC") - pd.Timedelta(days=1),
                freq="1D",
            )
        )
    return tuple(sorted(values))


def _download(url: str, *, retries: int) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "SMC-trading-system-research/1.0"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
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
    return pd.DataFrame(
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


def _fetch_one(
    item: tuple[str, pd.Timestamp],
    *,
    retries: int,
) -> tuple[str, pd.Timestamp, pd.DataFrame, str]:
    symbol, day = item
    date_text = day.strftime("%Y-%m-%d")
    archive_name = f"{symbol}-5m-{date_text}.zip"
    url = f"{ROOT}/{symbol}/5m/{archive_name}"
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
            f"{symbol} {date_text}: expected 288 bars, got {len(frame)}"
        )
    return symbol, day, frame, url


def main() -> int:
    args = _args()
    if args.workers <= 0:
        raise SystemExit("workers must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dates = _dates()
    tasks = [(symbol, day) for symbol in SYMBOLS for day in dates]
    by_symbol: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in SYMBOLS}
    manifest: list[dict[str, object]] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = {
            executor.submit(_fetch_one, item, retries=args.retries): item
            for item in tasks
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            symbol, day, frame, url = future.result()
            by_symbol[symbol].append(frame)
            completed += 1
            print(
                f"[{completed}/{len(tasks)}] {symbol} {day.date()}",
                flush=True,
            )
            manifest.append(
                {
                    "symbol": symbol,
                    "date": day.strftime("%Y-%m-%d"),
                    "bars": len(frame),
                    "url": url,
                }
            )

    expected = len(dates) * 288
    for symbol, pieces in by_symbol.items():
        output = (
            pd.concat(pieces, ignore_index=True)
            .drop_duplicates(subset=["open_time"], keep="last")
            .sort_values("open_time")
        )
        if len(output) != expected:
            raise AssertionError(
                f"{symbol}: expected {expected} bars, got {len(output)}"
            )
        output.to_csv(args.output_dir / f"{symbol}_5m.csv", index=False)
        print(f"wrote {symbol}: {len(output)} bars", flush=True)

    pd.DataFrame(manifest).sort_values(["symbol", "date"]).to_csv(
        args.output_dir / "download_manifest.csv",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "symbol": symbol,
                "operating_days": len(dates),
                "bars": expected,
            }
            for symbol in SYMBOLS
        ]
    ).to_csv(args.output_dir / "universe_summary.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
