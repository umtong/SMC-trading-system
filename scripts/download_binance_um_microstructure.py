from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request
import zipfile

import pandas as pd

from ictbt.microstructure import (
    aggregate_trade_flow,
    normalize_aggtrades,
    normalize_funding_rates,
)


ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly"
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
    parser = argparse.ArgumentParser(
        description=(
            "Download checksum-verified Binance USD-M aggTrades, fundingRate, "
            "and 1m mark-price archives for causal microstructure research."
        )
    )
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", required=True, help="inclusive UTC date")
    parser.add_argument("--end", required=True, help="exclusive UTC date")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--keep-archives", action="store_true")
    return parser.parse_args()


def _utc_date(value: str, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be a valid timestamp")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.normalize()


def _months(start: pd.Timestamp, end: pd.Timestamp) -> tuple[str, ...]:
    if end <= start:
        raise ValueError("end must follow start")
    final = (end - pd.Timedelta(nanoseconds=1)).to_period("M")
    first = start.to_period("M")
    return tuple(str(item) for item in pd.period_range(first, final, freq="M"))


def _fetch(url: str, *, retries: int) -> bytes:
    if retries <= 0:
        raise ValueError("retries must be positive")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "smc-trading-system-v09-research/1.0"},
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
    text = payload.decode("utf-8-sig").strip()
    token = text.split()[0].lower()
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError(f"invalid SHA-256 checksum payload: {text!r}")
    return token


def _archive_url(kind: str, symbol: str, month: str) -> str:
    if kind == "aggTrades":
        return f"{ARCHIVE_ROOT}/aggTrades/{symbol}/{symbol}-aggTrades-{month}.zip"
    if kind == "fundingRate":
        return f"{ARCHIVE_ROOT}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
    if kind == "markPriceKlines":
        return (
            f"{ARCHIVE_ROOT}/markPriceKlines/{symbol}/1m/"
            f"{symbol}-1m-{month}.zip"
        )
    raise ValueError(f"unknown archive kind: {kind}")


def _download_archive(
    *,
    kind: str,
    symbol: str,
    month: str,
    retries: int,
    raw_dir: Path | None,
) -> tuple[bytes, dict[str, object]]:
    url = _archive_url(kind, symbol, month)
    checksum_url = f"{url}.CHECKSUM"
    print(f"download {url}", flush=True)
    expected = _checksum(_fetch(checksum_url, retries=retries))
    payload = _fetch(url, retries=retries)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {url}: {actual} != {expected}")
    if raw_dir is not None:
        path = raw_dir / kind / symbol / Path(url).name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.with_suffix(path.suffix + ".CHECKSUM").write_text(
            f"{actual}  {path.name}\n",
            encoding="utf-8",
        )
    return payload, {
        "kind": kind,
        "symbol": symbol,
        "month": month,
        "url": url,
        "checksum_url": checksum_url,
        "sha256": actual,
        "archive_bytes": len(payload),
    }


def _csv_members(payload: bytes) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = sorted(
            name for name in archive.namelist() if name.lower().endswith(".csv")
        )
        if not members:
            raise ValueError("archive contains no CSV")
        for member in members:
            with archive.open(member) as handle:
                frames.append(pd.read_csv(handle, header=None, low_memory=False))
    return frames


def _timestamp_unit(values: pd.Series) -> str:
    maximum = int(values.max())
    if maximum < 10**11:
        return "s"
    if maximum < 10**14:
        return "ms"
    if maximum < 10**17:
        return "us"
    return "ns"


def _normalize_mark_klines(payload: bytes, *, symbol: str) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    for raw in _csv_members(payload):
        if raw.shape[1] < len(KLINE_COLUMNS):
            raise ValueError("mark-price archive has too few columns")
        frame = raw.iloc[:, : len(KLINE_COLUMNS)].copy()
        frame.columns = KLINE_COLUMNS
        numeric_time = pd.to_numeric(frame["open_time"], errors="coerce")
        if pd.isna(numeric_time.iloc[0]):
            frame = frame.iloc[1:].copy()
            numeric_time = pd.to_numeric(frame["open_time"], errors="raise")
        else:
            numeric_time = numeric_time.astype("int64")
        numeric_time = numeric_time.astype("int64")
        frame["open_time"] = pd.to_datetime(
            numeric_time,
            unit=_timestamp_unit(numeric_time),
            utc=True,
        )
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
        chunks.append(frame[["open_time", "open", "high", "low", "close"]])
    merged = pd.concat(chunks, ignore_index=True).sort_values("open_time", kind="mergesort")
    if merged["open_time"].duplicated().any():
        raise ValueError(f"{symbol}: duplicate mark-price timestamps")
    if bool((merged[["open", "high", "low", "close"]] <= 0).any().any()):
        raise ValueError(f"{symbol}: mark-price values must be positive")
    bad = (
        (merged["high"] < merged[["open", "close", "low"]].max(axis=1))
        | (merged["low"] > merged[["open", "close", "high"]].min(axis=1))
    )
    if bool(bad.any()):
        raise ValueError(f"{symbol}: invalid mark-price OHLC rows={int(bad.sum())}")
    merged.insert(0, "symbol", symbol)
    return merged.set_index("open_time")


def _normalize_agg_archive(payload: bytes, *, symbol: str) -> pd.DataFrame:
    chunks = [normalize_aggtrades(raw, symbol=symbol) for raw in _csv_members(payload)]
    merged = pd.concat(chunks).sort_index(kind="mergesort")
    if merged["agg_trade_id"].duplicated().any():
        raise ValueError(f"{symbol}: duplicate aggregate trade ids across archive members")
    if not merged["agg_trade_id"].is_monotonic_increasing:
        raise ValueError(f"{symbol}: aggregate trade ids reverse across archive members")
    return merged


def _normalize_funding_archive(payload: bytes, *, symbol: str) -> pd.DataFrame:
    chunks = [normalize_funding_rates(raw, symbol=symbol) for raw in _csv_members(payload)]
    merged = pd.concat(chunks).sort_index(kind="mergesort")
    if merged.index.duplicated().any():
        raise ValueError(f"{symbol}: duplicate funding times across archive members")
    return merged


def _half_open(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame.loc[(frame.index >= start) & (frame.index < end)].copy()


def _write_frame(frame: pd.DataFrame, path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.reset_index()
    for column in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[column]):
            output[column] = output[column].map(lambda value: value.isoformat())
    output.to_csv(path, index=False, compression="gzip")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path),
        "rows": int(len(output)),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def _validate_minute_clock(
    frame: pd.DataFrame,
    *,
    symbol: str,
    label: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    actual = pd.DatetimeIndex(frame.index)
    missing = expected.difference(actual)
    outside = actual[(actual < start) | (actual >= end)]
    if len(outside):
        raise ValueError(f"{symbol}/{label}: observations outside requested window")
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{symbol}/{label}: missing {len(missing)} minute observations; first={preview}"
        )
    if len(actual) != len(expected):
        raise ValueError(
            f"{symbol}/{label}: row count {len(actual)} != expected {len(expected)}"
        )


def main() -> int:
    args = _args()
    start = _utc_date(args.start, name="start")
    end = _utc_date(args.end, name="end")
    months = _months(start, end)
    symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in args.symbols))
    if not symbols or any(not symbol for symbol in symbols):
        raise ValueError("at least one valid symbol is required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw" if args.keep_archives else None

    source_manifest: list[dict[str, object]] = []
    output_manifest: list[dict[str, object]] = []
    symbol_summary: list[dict[str, object]] = []

    for symbol in symbols:
        flow_1s_chunks: list[pd.DataFrame] = []
        flow_1m_chunks: list[pd.DataFrame] = []
        funding_chunks: list[pd.DataFrame] = []
        mark_chunks: list[pd.DataFrame] = []
        aggregate_rows = 0

        for month in months:
            agg_payload, agg_meta = _download_archive(
                kind="aggTrades",
                symbol=symbol,
                month=month,
                retries=args.retries,
                raw_dir=raw_dir,
            )
            trades = _normalize_agg_archive(agg_payload, symbol=symbol)
            aggregate_rows += len(trades)
            flow_1s_chunks.append(aggregate_trade_flow(trades, frequency="1s"))
            flow_1m_chunks.append(aggregate_trade_flow(trades, frequency="1min"))
            agg_meta["rows"] = int(len(trades))
            source_manifest.append(agg_meta)
            del trades, agg_payload

            funding_payload, funding_meta = _download_archive(
                kind="fundingRate",
                symbol=symbol,
                month=month,
                retries=args.retries,
                raw_dir=raw_dir,
            )
            funding = _normalize_funding_archive(funding_payload, symbol=symbol)
            funding_chunks.append(funding)
            funding_meta["rows"] = int(len(funding))
            source_manifest.append(funding_meta)
            del funding_payload

            mark_payload, mark_meta = _download_archive(
                kind="markPriceKlines",
                symbol=symbol,
                month=month,
                retries=args.retries,
                raw_dir=raw_dir,
            )
            mark = _normalize_mark_klines(mark_payload, symbol=symbol)
            mark_chunks.append(mark)
            mark_meta["rows"] = int(len(mark))
            source_manifest.append(mark_meta)
            del mark_payload

        flow_1s = _half_open(
            pd.concat(flow_1s_chunks).sort_index(kind="mergesort"), start, end
        )
        flow_1m = _half_open(
            pd.concat(flow_1m_chunks).sort_index(kind="mergesort"), start, end
        )
        funding = _half_open(
            pd.concat(funding_chunks).sort_index(kind="mergesort"), start, end
        )
        mark = _half_open(
            pd.concat(mark_chunks).sort_index(kind="mergesort"), start, end
        )
        for label, frame in (("flow_1m", flow_1m), ("mark_1m", mark)):
            if frame.index.duplicated().any():
                raise ValueError(f"{symbol}/{label}: duplicate timestamps")
            _validate_minute_clock(
                frame,
                symbol=symbol,
                label=label,
                start=start,
                end=end,
            )
        if funding.empty:
            raise ValueError(f"{symbol}: requested range contains no funding observations")

        symbol_dir = args.output_dir / "normalized" / symbol
        outputs = {
            "flow_1s": _write_frame(flow_1s, symbol_dir / "aggtrade_flow_1s.csv.gz"),
            "flow_1m": _write_frame(flow_1m, symbol_dir / "aggtrade_flow_1m.csv.gz"),
            "funding": _write_frame(funding, symbol_dir / "funding_rate.csv.gz"),
            "mark_1m": _write_frame(mark, symbol_dir / "mark_price_1m.csv.gz"),
        }
        for kind, metadata in outputs.items():
            output_manifest.append({"symbol": symbol, "kind": kind, **metadata})
        symbol_summary.append(
            {
                "symbol": symbol,
                "aggregate_trade_rows": int(aggregate_rows),
                "flow_1s_rows": int(len(flow_1s)),
                "flow_1m_rows": int(len(flow_1m)),
                "funding_rows": int(len(funding)),
                "mark_1m_rows": int(len(mark)),
                "first_flow": flow_1m.index[0].isoformat(),
                "last_flow": flow_1m.index[-1].isoformat(),
            }
        )
        print(json.dumps(symbol_summary[-1], ensure_ascii=False), flush=True)

    manifest = {
        "schema_version": 1,
        "source": "Binance public data USD-M monthly archives",
        "archive_root": ARCHIVE_ROOT,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": list(symbols),
        "months": list(months),
        "buyer_maker_sign_contract": {
            "false": "buyer taker, positive signed quote volume",
            "true": "seller taker, negative signed quote volume",
        },
        "sources": source_manifest,
        "outputs": output_manifest,
        "symbols_summary": symbol_summary,
    }
    manifest_path = args.output_dir / "microstructure_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(symbol_summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
