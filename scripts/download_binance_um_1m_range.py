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
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="inclusive UTC boundary")
    parser.add_argument("--end", required=True, help="exclusive UTC boundary")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--keep-archives", action="store_true")
    return parser.parse_args()


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be valid")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def _months(start: object, end: object) -> tuple[str, ...]:
    begin = _utc(start, name="start")
    finish = _utc(end, name="end")
    if finish <= begin:
        raise ValueError("end must follow start")
    first = begin.tz_localize(None).to_period("M")
    last = (finish - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return tuple(str(item) for item in pd.period_range(first, last, freq="M"))


def _archive_url(symbol: str, month: str) -> str:
    filename = f"{symbol}-1m-{month}.zip"
    return f"{ARCHIVE_ROOT}/{symbol}/1m/{filename}"


def _fetch(url: str, *, retries: int) -> bytes:
    if retries <= 0:
        raise ValueError("retries must be positive")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "smc-trading-system-v10-1m/1.0"},
    )
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 == retries:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"download failed after {retries} attempts: {url}") from last


def _checksum(payload: bytes) -> str:
    token = payload.decode("utf-8-sig").strip().split()[0].lower()
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("invalid SHA-256 checksum payload")
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


def _read_archive(payload: bytes, *, symbol: str, month: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = sorted(name for name in archive.namelist() if name.lower().endswith(".csv"))
        if not members:
            raise ValueError(f"{symbol}/{month}: archive contains no CSV")
        for member in members:
            with archive.open(member) as handle:
                raw = pd.read_csv(handle, header=None, low_memory=False)
            if raw.shape[1] < len(KLINE_COLUMNS):
                raise ValueError(f"{symbol}/{month}: unexpected kline columns={raw.shape[1]}")
            frame = raw.iloc[:, : len(KLINE_COLUMNS)].copy()
            frame.columns = KLINE_COLUMNS
            numeric_time = pd.to_numeric(frame["open_time"], errors="coerce")
            if pd.isna(numeric_time.iloc[0]):
                frame = frame.iloc[1:].copy()
                numeric_time = pd.to_numeric(frame["open_time"], errors="raise")
            numeric_time = numeric_time.astype("int64")
            frame["open_time"] = pd.to_datetime(
                numeric_time,
                unit=_timestamp_unit(numeric_time),
                utc=True,
            )
            for column in ("open", "high", "low", "close", "volume"):
                frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
            frames.append(frame[["open_time", "open", "high", "low", "close", "volume"]])
    merged = pd.concat(frames, ignore_index=True).sort_values("open_time", kind="mergesort")
    if merged["open_time"].duplicated().any():
        raise ValueError(f"{symbol}/{month}: duplicate timestamps inside archive")
    prices = merged[["open", "high", "low", "close"]]
    if not np.isfinite(merged[["open", "high", "low", "close", "volume"]].to_numpy()).all():
        raise ValueError(f"{symbol}/{month}: non-finite OHLCV")
    if bool((prices <= 0).any().any()) or bool((merged["volume"] < 0).any()):
        raise ValueError(f"{symbol}/{month}: non-positive prices or negative volume")
    bad = (
        (merged["high"] < merged[["open", "close", "low"]].max(axis=1))
        | (merged["low"] > merged[["open", "close", "high"]].min(axis=1))
    )
    if bool(bad.any()):
        raise ValueError(f"{symbol}/{month}: invalid OHLC ordering={int(bad.sum())}")
    return merged.set_index("open_time")


def _validate_range(
    frame: pd.DataFrame,
    *,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    selected = frame.loc[(frame.index >= start) & (frame.index < end)].copy()
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    missing = expected.difference(selected.index)
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{symbol}: missing {len(missing)} of {len(expected)} 1m bars; first={preview}"
        )
    if len(selected) != len(expected):
        raise ValueError(f"{symbol}: rows={len(selected)} != expected={len(expected)}")
    return selected


def main() -> int:
    args = _args()
    start = _utc(args.start, name="start")
    end = _utc(args.end, name="end")
    if start.second or start.microsecond or start.nanosecond:
        raise ValueError("start must align to an exact-minute UTC boundary")
    if end.second or end.microsecond or end.nanosecond:
        raise ValueError("end must align to an exact-minute UTC boundary")
    months = _months(start, end)
    symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in args.symbols))
    if not symbols or any(not item for item in symbols):
        raise ValueError("at least one symbol is required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []

    for symbol in symbols:
        chunks: list[pd.DataFrame] = []
        for month in months:
            url = _archive_url(symbol, month)
            checksum_url = f"{url}.CHECKSUM"
            print(f"download {url}", flush=True)
            expected_sha = _checksum(_fetch(checksum_url, retries=args.retries))
            payload = _fetch(url, retries=args.retries)
            actual_sha = hashlib.sha256(payload).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"{symbol}/{month}: checksum mismatch {actual_sha} != {expected_sha}"
                )
            frame = _read_archive(payload, symbol=symbol, month=month)
            chunks.append(frame)
            if args.keep_archives:
                raw_path = args.output_dir / "raw" / symbol / Path(url).name
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(payload)
                raw_path.with_suffix(raw_path.suffix + ".CHECKSUM").write_text(
                    f"{actual_sha}  {raw_path.name}\n", encoding="utf-8"
                )
            sources.append(
                {
                    "symbol": symbol,
                    "month": month,
                    "url": url,
                    "checksum_url": checksum_url,
                    "sha256": actual_sha,
                    "archive_bytes": len(payload),
                    "archive_rows": int(len(frame)),
                    "first_open_time": frame.index[0].isoformat(),
                    "last_open_time": frame.index[-1].isoformat(),
                }
            )
        merged = pd.concat(chunks).sort_index(kind="mergesort")
        if merged.index.duplicated().any():
            duplicate = merged.index[merged.index.duplicated()][0]
            raise ValueError(f"{symbol}: duplicate timestamp across archives: {duplicate}")
        selected = _validate_range(merged, symbol=symbol, start=start, end=end)
        output = selected.reset_index()
        output["open_time"] = output["open_time"].map(lambda value: value.isoformat())
        path = args.output_dir / f"{symbol}_1m.csv.gz"
        output.to_csv(path, index=False, compression="gzip")
        outputs.append(
            {
                "symbol": symbol,
                "path": str(path),
                "rows": int(len(output)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
                "first_open_time": output["open_time"].iloc[0],
                "last_open_time": output["open_time"].iloc[-1],
            }
        )

    manifest = {
        "schema_version": 1,
        "source": "Binance USD-M public monthly 1m kline archives",
        "archive_root": ARCHIVE_ROOT,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months": list(months),
        "symbols": list(symbols),
        "expected_rows_per_symbol": int(
            len(pd.date_range(start, end, freq="1min", inclusive="left"))
        ),
        "sources": sources,
        "outputs": outputs,
    }
    (args.output_dir / "v10_1m_data_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(outputs, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
