from __future__ import annotations

import argparse
import io
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

WINDOWS = (
    ("ETHUSDT", "2024-12-30", "2025-01-13"),
    ("ETHUSDT", "2025-07-08", "2025-07-22"),
    ("BTCUSDT", "2026-01-23", "2026-02-06"),
    ("ETHUSDT", "2026-01-23", "2026-02-06"),
    ("BTCUSDT", "2026-04-04", "2026-04-18"),
    ("BTCUSDT", "2026-06-08", "2026-06-22"),
)

MARKET_ROOTS = {
    "um_futures": "https://data.binance.vision/data/futures/um/daily/klines",
    "spot": "https://data.binance.vision/data/spot/daily/klines",
}

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
    parser.add_argument("--market", choices=tuple(MARKET_ROOTS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def _dates(start: str, end: str) -> tuple[pd.Timestamp, ...]:
    return tuple(
        pd.date_range(
            pd.Timestamp(start, tz="UTC"),
            pd.Timestamp(end, tz="UTC") - pd.Timedelta(days=1),
            freq="1D",
        )
    )


def _download(url: str, *, retries: int) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "SMC-trading-system-reproduction/1.0"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    assert last is not None
    raise RuntimeError(f"failed to download {url}: {last}") from last


def _timestamp_unit(values: pd.Series) -> str:
    numeric = pd.to_numeric(values, errors="raise")
    maximum = int(numeric.max())
    if maximum >= 10**17:
        return "ns"
    if maximum >= 10**14:
        return "us"
    if maximum >= 10**11:
        return "ms"
    return "s"


def _read_archive(payload: bytes, *, expected_name: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError(f"empty archive: {expected_name}")
        with archive.open(names[0]) as stream:
            raw = pd.read_csv(stream, header=None)
    if raw.empty:
        raise ValueError(f"empty CSV: {expected_name}")
    if not str(raw.iloc[0, 0]).strip().lstrip("-").isdigit():
        raw = raw.iloc[1:].reset_index(drop=True)
    if raw.shape[1] < 6:
        raise ValueError(f"unexpected Binance kline shape {raw.shape}: {expected_name}")
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
    return frame


def main() -> int:
    args = _args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = MARKET_ROOTS[args.market]
    by_symbol: dict[str, set[pd.Timestamp]] = {}
    for symbol, start, end in WINDOWS:
        by_symbol.setdefault(symbol, set()).update(_dates(start, end))

    manifest: list[dict[str, object]] = []
    for symbol, dates in sorted(by_symbol.items()):
        pieces: list[pd.DataFrame] = []
        for index, day in enumerate(sorted(dates), start=1):
            date_text = day.strftime("%Y-%m-%d")
            archive_name = f"{symbol}-5m-{date_text}.zip"
            url = f"{root}/{symbol}/5m/{archive_name}"
            print(
                f"[{args.market}] {symbol} {index}/{len(dates)} {date_text}",
                flush=True,
            )
            payload = _download(url, retries=args.retries)
            frame = _read_archive(payload, expected_name=archive_name)
            expected_start = day
            expected_end = day + pd.Timedelta(days=1)
            frame = frame.loc[
                (frame["open_time"] >= expected_start)
                & (frame["open_time"] < expected_end)
            ].copy()
            if len(frame) != 288:
                raise AssertionError(
                    f"{args.market} {symbol} {date_text}: expected 288 bars, got {len(frame)}"
                )
            pieces.append(frame)
            manifest.append(
                {
                    "market": args.market,
                    "symbol": symbol,
                    "date": date_text,
                    "bars": len(frame),
                    "url": url,
                }
            )
        output = (
            pd.concat(pieces, ignore_index=True)
            .drop_duplicates(subset=["open_time"], keep="last")
            .sort_values("open_time")
        )
        output.to_csv(args.output_dir / f"{symbol}_5m.csv", index=False)
        print(f"wrote {symbol}: {len(output)} bars", flush=True)

    pd.DataFrame(manifest).to_csv(
        args.output_dir / "download_manifest.csv",
        index=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
