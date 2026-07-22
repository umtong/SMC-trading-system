from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

from scripts.v08_windows import ALL_WINDOWS


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
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument("--retries", type=int, default=5)
    return parser.parse_args()


def _fetch(url: str, *, retries: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "smc-trading-system-v08-research/1.0"},
    )
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 == retries:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"download failed after {retries} attempts: {url}") from last


def _checksum_value(payload: bytes) -> str:
    text = payload.decode("utf-8-sig").strip()
    token = text.split()[0].lower()
    if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
        raise ValueError(f"invalid SHA-256 checksum payload: {text!r}")
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


def _read_zip(payload: bytes) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            raise ValueError("archive contains no CSV")
        for member in members:
            with archive.open(member) as handle:
                raw = pd.read_csv(handle, header=None, low_memory=False)
            if raw.shape[1] < 6:
                raise ValueError(f"unexpected kline column count in {member}: {raw.shape[1]}")
            raw = raw.iloc[:, : len(KLINE_COLUMNS)].copy()
            raw.columns = KLINE_COLUMNS[: raw.shape[1]]
            numeric_time = pd.to_numeric(raw["open_time"], errors="coerce")
            raw = raw.loc[numeric_time.notna()].copy()
            numeric_time = numeric_time.loc[numeric_time.notna()].astype("int64")
            raw["open_time"] = pd.to_datetime(
                numeric_time,
                unit=_timestamp_unit(numeric_time),
                utc=True,
            )
            for column in ("open", "high", "low", "close", "volume"):
                raw[column] = pd.to_numeric(raw[column], errors="raise")
            frames.append(raw[["open_time", "open", "high", "low", "close", "volume"]])
    return pd.concat(frames, ignore_index=True)


def _months_for_window(start: str, end: str) -> tuple[str, ...]:
    begin = pd.Timestamp(start, tz="UTC").to_period("M")
    finish = (pd.Timestamp(end, tz="UTC") - pd.Timedelta(nanoseconds=1)).to_period("M")
    return tuple(str(item) for item in pd.period_range(begin, finish, freq="M"))


def _validate_window(
    frame: pd.DataFrame,
    *,
    symbol: str,
    environment: str,
    start: str,
    end: str,
) -> dict[str, object]:
    begin = pd.Timestamp(start, tz="UTC")
    finish = pd.Timestamp(end, tz="UTC")
    window = frame.loc[(frame.index >= begin) & (frame.index < finish)]
    expected = pd.date_range(begin, finish, freq="5min", inclusive="left")
    missing = expected.difference(window.index)
    duplicates = int(window.index.duplicated().sum())
    if duplicates:
        raise ValueError(f"{environment}: duplicate 5m bars={duplicates}")
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{environment}: missing {len(missing)} of {len(expected)} 5m bars; first={preview}"
        )
    if len(window) != len(expected):
        raise ValueError(
            f"{environment}: unexpected row count {len(window)} != {len(expected)}"
        )
    bad_ohlc = (
        (window["high"] < window[["open", "close", "low"]].max(axis=1))
        | (window["low"] > window[["open", "close", "high"]].min(axis=1))
        | (window[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (window["volume"] < 0)
    )
    if bool(bad_ohlc.any()):
        raise ValueError(f"{environment}: invalid OHLCV rows={int(bad_ohlc.sum())}")
    return {
        "symbol": symbol,
        "environment": environment,
        "start": begin.isoformat(),
        "end": finish.isoformat(),
        "rows": int(len(window)),
        "missing": 0,
        "duplicates": 0,
    }


def main() -> int:
    args = _args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    needed: dict[str, set[str]] = defaultdict(set)
    for symbol, _environment, start, end, _tick in ALL_WINDOWS:
        needed[symbol].update(_months_for_window(start, end))

    source_manifest: list[dict[str, object]] = []
    symbol_frames: dict[str, pd.DataFrame] = {}
    for symbol, months in sorted(needed.items()):
        chunks: list[pd.DataFrame] = []
        for month in sorted(months):
            filename = f"{symbol}-5m-{month}.zip"
            url = f"{ARCHIVE_ROOT}/{symbol}/5m/{filename}"
            checksum_url = f"{url}.CHECKSUM"
            print(f"download {url}", flush=True)
            checksum_payload = _fetch(checksum_url, retries=args.retries)
            expected_sha = _checksum_value(checksum_payload)
            payload = _fetch(url, retries=args.retries)
            actual_sha = hashlib.sha256(payload).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"checksum mismatch for {filename}: {actual_sha} != {expected_sha}"
                )
            chunk = _read_zip(payload)
            chunks.append(chunk)
            source_manifest.append(
                {
                    "symbol": symbol,
                    "month": month,
                    "url": url,
                    "checksum_url": checksum_url,
                    "sha256": actual_sha,
                    "archive_bytes": len(payload),
                    "rows": int(len(chunk)),
                }
            )
        merged = pd.concat(chunks, ignore_index=True)
        merged = merged.sort_values("open_time", kind="mergesort")
        duplicate_count = int(merged["open_time"].duplicated().sum())
        if duplicate_count:
            raise ValueError(f"{symbol}: duplicate source timestamps={duplicate_count}")
        merged = merged.set_index("open_time")
        symbol_frames[symbol] = merged

    window_manifest = [
        _validate_window(
            symbol_frames[symbol],
            symbol=symbol,
            environment=environment,
            start=start,
            end=end,
        )
        for symbol, environment, start, end, _tick in ALL_WINDOWS
    ]

    output_manifest: list[dict[str, object]] = []
    for symbol, frame in sorted(symbol_frames.items()):
        masks = []
        for item_symbol, _environment, start, end, _tick in ALL_WINDOWS:
            if item_symbol != symbol:
                continue
            begin = pd.Timestamp(start, tz="UTC")
            finish = pd.Timestamp(end, tz="UTC")
            masks.append((frame.index >= begin) & (frame.index < finish))
        union = masks[0].copy()
        for mask in masks[1:]:
            union |= mask
        output = frame.loc[union].copy().reset_index()
        output["open_time"] = output["open_time"].map(lambda value: value.isoformat())
        path = args.output_dir / f"{symbol}_5m.csv"
        output.to_csv(path, index=False)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        output_manifest.append(
            {
                "symbol": symbol,
                "path": str(path),
                "rows": int(len(output)),
                "sha256": digest,
            }
        )

    manifest = {
        "source": "Binance USD-M public monthly kline archives",
        "interval": "5m",
        "sources": source_manifest,
        "windows": window_manifest,
        "outputs": output_manifest,
    }
    manifest_path = args.output_dir / "v08_data_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest["outputs"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
