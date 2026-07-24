from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile

import numpy as np
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
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    return parser.parse_args()


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be valid")
    return timestamp.tz_localize("UTC") if timestamp.tz is None else timestamp.tz_convert("UTC")


def _months(start: pd.Timestamp, end: pd.Timestamp) -> tuple[str, ...]:
    if end <= start:
        raise ValueError("end must follow start")
    first = start.tz_localize(None).to_period("M")
    last = (end - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return tuple(str(item) for item in pd.period_range(first, last, freq="M"))


def _url(symbol: str, month: str) -> str:
    name = f"{symbol}-1m-{month}.zip"
    return f"{ARCHIVE_ROOT}/{symbol}/1m/{name}"


def _fetch(url: str, retries: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "smc-v10-flow/1.0"})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"download failed: {url}") from last


def _checksum(payload: bytes) -> str:
    token = payload.decode("utf-8-sig").strip().split()[0].lower()
    if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
        raise ValueError("invalid SHA-256 checksum")
    return token


def _timestamp_unit(values: pd.Series) -> str:
    maximum = int(values.max())
    if maximum < 10**11:
        return "s"
    if maximum < 10**14:
        return "ms"
    if maximum < 10**17:
        return "us"
    return "ns"


def _read(payload: bytes, symbol: str, month: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
        if not members:
            raise ValueError(f"{symbol}/{month}: no CSV")
        frames: list[pd.DataFrame] = []
        for member in members:
            raw = pd.read_csv(archive.open(member), header=None, low_memory=False)
            if raw.shape[1] < len(KLINE_COLUMNS):
                raise ValueError(f"{symbol}/{month}: columns={raw.shape[1]}")
            frame = raw.iloc[:, : len(KLINE_COLUMNS)].copy()
            frame.columns = KLINE_COLUMNS
            numeric_time = pd.to_numeric(frame.open_time, errors="coerce")
            if pd.isna(numeric_time.iloc[0]):
                frame = frame.iloc[1:].copy()
                numeric_time = pd.to_numeric(frame.open_time, errors="raise")
            numeric_time = numeric_time.astype("int64")
            frame["open_time"] = pd.to_datetime(
                numeric_time, unit=_timestamp_unit(numeric_time), utc=True
            )
            numeric = (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_volume",
                "trade_count",
                "taker_buy_base",
                "taker_buy_quote",
            )
            for column in numeric:
                frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
            frames.append(frame[["open_time", *numeric]])
    merged = pd.concat(frames, ignore_index=True).sort_values("open_time", kind="mergesort")
    if merged.open_time.duplicated().any():
        raise ValueError(f"{symbol}/{month}: duplicate timestamps")
    values = merged.drop(columns="open_time").to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"{symbol}/{month}: non-finite values")
    bad_ohlc = (
        (merged.high < merged[["open", "close", "low"]].max(axis=1))
        | (merged.low > merged[["open", "close", "high"]].min(axis=1))
    )
    if bool(bad_ohlc.any()):
        raise ValueError(f"{symbol}/{month}: invalid OHLC")
    if bool((merged[["open", "high", "low", "close"]] <= 0).any().any()):
        raise ValueError(f"{symbol}/{month}: non-positive prices")
    if bool((merged[["volume", "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote"]] < 0).any().any()):
        raise ValueError(f"{symbol}/{month}: negative volume/count")
    if bool((merged.taker_buy_quote > merged.quote_volume + 1e-8).any()):
        raise ValueError(f"{symbol}/{month}: taker buy quote exceeds quote volume")
    return merged.set_index("open_time")


def main() -> int:
    args = _args()
    symbol = args.symbol.strip().upper()
    start = _utc(args.start, name="start")
    end = _utc(args.end, name="end")
    if start.second or start.microsecond or end.second or end.microsecond:
        raise ValueError("boundaries must align to exact minutes")
    chunks: list[pd.DataFrame] = []
    sources: list[dict[str, object]] = []
    for month in _months(start, end):
        url = _url(symbol, month)
        expected = _checksum(_fetch(url + ".CHECKSUM", args.retries))
        payload = _fetch(url, args.retries)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"{symbol}/{month}: checksum mismatch")
        frame = _read(payload, symbol, month)
        chunks.append(frame)
        sources.append(
            {
                "symbol": symbol,
                "month": month,
                "url": url,
                "checksum_url": url + ".CHECKSUM",
                "sha256": actual,
                "archive_bytes": len(payload),
                "archive_rows": len(frame),
            }
        )
        print(symbol, month, len(frame), actual, flush=True)
    frame = pd.concat(chunks).sort_index(kind="mergesort")
    if frame.index.has_duplicates:
        raise ValueError(f"{symbol}: duplicate timestamp across archives")
    selected = frame.loc[(frame.index >= start) & (frame.index < end)].copy()
    expected_index = pd.date_range(start, end, freq="1min", inclusive="left")
    missing = expected_index.difference(selected.index)
    if len(missing) or len(selected) != len(expected_index):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{symbol}: missing={len(missing)} rows={len(selected)} expected={len(expected_index)} first={preview}"
        )
    selected["taker_sell_quote"] = selected.quote_volume - selected.taker_buy_quote
    selected["signed_quote_volume"] = selected.taker_buy_quote - selected.taker_sell_quote
    selected["imbalance_fraction"] = selected.signed_quote_volume / selected.quote_volume.replace(0, np.nan)
    selected["average_trade_quote"] = selected.quote_volume / selected.trade_count.replace(0, np.nan)
    selected["price_change_bps"] = (selected.close / selected.open - 1.0) * 10_000.0
    selected["range_bps"] = (selected.high - selected.low) / selected.open * 10_000.0
    selected["close_location"] = (
        (selected.close - selected.low) - (selected.high - selected.close)
    ) / (selected.high - selected.low).replace(0, np.nan)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = selected.reset_index()
    output.open_time = output.open_time.map(lambda value: value.isoformat())
    path = args.output_dir / f"{symbol}_1m_flow.csv.gz"
    output.to_csv(path, index=False, compression="gzip")
    manifest = {
        "schema_version": 1,
        "contract": "binance_usdm_1m_taker_flow",
        "source": "Binance USD-M monthly one-minute kline archives",
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "expected_rows": len(expected_index),
        "sources": sources,
        "output": {
            "path": str(path),
            "rows": len(output),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "columns": list(output.columns),
        },
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest["output"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
