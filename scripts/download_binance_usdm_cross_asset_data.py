from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

BASE_URL = "https://data.binance.vision/data/futures/um"
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ignore",
]


@dataclass(frozen=True)
class DownloadSpec:
    symbol: str
    data_type: str
    interval: str | None
    period: str
    cadence: str

    @property
    def filename(self) -> str:
        if self.data_type == "fundingRate":
            return f"{self.symbol}-fundingRate-{self.period}.zip"
        assert self.interval is not None
        return f"{self.symbol}-{self.interval}-{self.period}.zip"

    @property
    def url(self) -> str:
        if self.data_type == "fundingRate":
            return f"{BASE_URL}/{self.cadence}/fundingRate/{self.symbol}/{self.filename}"
        assert self.interval is not None
        return (
            f"{BASE_URL}/{self.cadence}/{self.data_type}/"
            f"{self.symbol}/{self.interval}/{self.filename}"
        )


def iter_months(start: str, end: str) -> Iterable[str]:
    year, month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def iter_days(start: str, end: str) -> Iterable[str]:
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current.isoformat()
        current += timedelta(days=1)


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(text: str) -> str:
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if match is None:
        raise ValueError(f"invalid CHECKSUM payload: {text[:120]!r}")
    return match.group(1).lower()


def fetch_one(
    spec: DownloadSpec,
    *,
    timeout: int = 120,
    retries: int = 4,
) -> tuple[DownloadSpec, bytes, dict[str, object]]:
    headers = {"User-Agent": "smc-ict-cross-asset-research/1.0"}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            checksum = requests.get(
                spec.url + ".CHECKSUM", headers=headers, timeout=timeout
            )
            checksum.raise_for_status()
            expected = parse_checksum(checksum.text)
            actual = sha256_bytes(blob)
            if actual != expected:
                raise ValueError(
                    f"checksum mismatch for {spec.filename}: {actual} != {expected}"
                )
            return spec, blob, {
                "url": spec.url,
                "filename": spec.filename,
                "bytes": len(blob),
                "sha256": actual,
                "attempt": attempt,
            }
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(min(2**attempt, 15))
    assert last_error is not None
    raise last_error


def read_zip_csv(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in archive, found {names}")
        raw = archive.read(names[0])
    first = raw.splitlines()[0].decode("utf-8", errors="replace").lower()
    has_header = any(token in first for token in ("open_time", "calc_time", "funding"))
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


def timestamp_unit(values: pd.Series) -> str:
    numeric = pd.to_numeric(values, errors="raise")
    return "us" if numeric.abs().median() > 10**14 else "ms"


def normalize_kline(frame: pd.DataFrame, spec: DownloadSpec) -> pd.DataFrame:
    if frame.shape[1] < 12:
        raise ValueError(f"{spec.filename}: expected 12 columns, got {frame.shape[1]}")
    output = frame.iloc[:, :12].copy()
    output.columns = KLINE_COLUMNS
    for column in KLINE_COLUMNS:
        output[column] = pd.to_numeric(output[column], errors="raise")
    unit = timestamp_unit(output["open_time"])
    output["open_time"] = pd.to_datetime(output["open_time"], unit=unit, utc=True)
    output["close_time"] = pd.to_datetime(output["close_time"], unit=unit, utc=True)
    output["symbol"] = spec.symbol
    output["source_type"] = "klines"
    output["interval"] = spec.interval
    return output


def normalize_funding(frame: pd.DataFrame, spec: DownloadSpec) -> pd.DataFrame:
    if frame.shape[1] < 2:
        raise ValueError(f"{spec.filename}: funding file has too few columns")
    lower = [str(column).strip().lower() for column in frame.columns]
    if "calc_time" in lower:
        time_column = frame.columns[lower.index("calc_time")]
    elif "fundingtime" in lower:
        time_column = frame.columns[lower.index("fundingtime")]
    else:
        time_column = frame.columns[0]
    rate_columns = [column for column in frame.columns if "rate" in str(column).lower()]
    rate_column = rate_columns[-1] if rate_columns else frame.columns[-1]
    times = pd.to_numeric(frame[time_column], errors="raise")
    output = pd.DataFrame(
        {
            "calc_time": pd.to_datetime(
                times, unit=timestamp_unit(times), utc=True
            ),
            "funding_rate": pd.to_numeric(frame[rate_column], errors="raise"),
            "symbol": spec.symbol,
        }
    )
    return output


def validate_kline(frame: pd.DataFrame) -> dict[str, object]:
    if frame.empty:
        raise ValueError("empty kline frame")
    ordered = frame.sort_values("open_time")
    duplicates = int(ordered["open_time"].duplicated().sum())
    if duplicates:
        raise ValueError(f"duplicate kline timestamps: {duplicates}")
    bad_ohlc = int(
        (
            (ordered["high"] < ordered[["open", "low", "close"]].max(axis=1))
            | (ordered["low"] > ordered[["open", "high", "close"]].min(axis=1))
            | (ordered[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (ordered["volume"] < 0)
        ).sum()
    )
    if bad_ohlc:
        raise ValueError(f"invalid OHLCV rows: {bad_ohlc}")
    deltas = ordered["open_time"].diff().dropna()
    irregular = deltas[deltas != pd.Timedelta(minutes=5)]
    gap_ranges = [
        {
            "previous": ordered.iloc[index - 1]["open_time"].isoformat(),
            "next": ordered.iloc[index]["open_time"].isoformat(),
            "delta_seconds": float(delta.total_seconds()),
        }
        for index, delta in zip(irregular.index[:100], irregular.iloc[:100])
    ]
    return {
        "rows": int(len(ordered)),
        "start": ordered["open_time"].min().isoformat(),
        "end": ordered["open_time"].max().isoformat(),
        "duplicates": duplicates,
        "irregular_intervals": int(len(irregular)),
        "gap_ranges_sample": gap_ranges,
        "zero_volume_rows": int((ordered["volume"] == 0).sum()),
    }


def build_specs(
    symbols: list[str],
    start_month: str,
    end_month: str,
    daily_start: str | None,
    daily_end: str | None,
) -> list[DownloadSpec]:
    specs: list[DownloadSpec] = []
    for symbol in symbols:
        for month in iter_months(start_month, end_month):
            specs.append(DownloadSpec(symbol, "klines", "5m", month, "monthly"))
            specs.append(DownloadSpec(symbol, "fundingRate", None, month, "monthly"))
        if daily_start and daily_end:
            for day in iter_days(daily_start, daily_end):
                specs.append(DownloadSpec(symbol, "klines", "5m", day, "daily"))
    return specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=[
            "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
            "LINKUSDT", "LTCUSDT", "BCHUSDT", "AVAXUSDT",
        ],
    )
    parser.add_argument("--start-month", default="2020-01")
    parser.add_argument("--end-month", default="2026-06")
    parser.add_argument("--daily-start", default="2026-07-01")
    parser.add_argument("--daily-end", default="2026-07-21")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/binance_usdm_cross_asset_data"),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    specs = build_specs(
        args.symbols, args.start_month, args.end_month,
        args.daily_start, args.daily_end,
    )
    frames: dict[tuple[str, str], list[pd.DataFrame]] = {}
    manifest: list[dict[str, object]] = []
    missing: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, spec): spec for spec in specs}
        for ordinal, future in enumerate(concurrent.futures.as_completed(futures), 1):
            spec = futures[future]
            try:
                returned, blob, metadata = future.result()
            except FileNotFoundError:
                missing.append(spec.url)
                continue
            raw = read_zip_csv(blob)
            normalized = (
                normalize_funding(raw, returned)
                if returned.data_type == "fundingRate"
                else normalize_kline(raw, returned)
            )
            frames.setdefault((returned.symbol, returned.data_type), []).append(normalized)
            manifest.append({**asdict(returned), **metadata})
            if ordinal % 100 == 0:
                print(f"processed {ordinal}/{len(specs)}", flush=True)

    datasets: dict[str, dict[str, object]] = {}
    for (symbol, data_type), chunks in sorted(frames.items()):
        combined = pd.concat(chunks, ignore_index=True)
        time_column = "calc_time" if data_type == "fundingRate" else "open_time"
        combined = (
            combined.sort_values(time_column)
            .drop_duplicates(time_column, keep="last")
            .reset_index(drop=True)
        )
        target = args.output / f"{symbol}_{data_type}.parquet"
        combined.to_parquet(target, index=False, compression="zstd")
        info: dict[str, object] = {
            "path": str(target),
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
            "rows": int(len(combined)),
            "start": combined[time_column].min().isoformat(),
            "end": combined[time_column].max().isoformat(),
        }
        if data_type == "klines":
            info.update(validate_kline(combined))
        datasets[f"{symbol}_{data_type}"] = info

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE_URL,
        "symbols": args.symbols,
        "start_month": args.start_month,
        "end_month": args.end_month,
        "daily_start": args.daily_start,
        "daily_end": args.daily_end,
        "archives": sorted(
            manifest,
            key=lambda item: (
                str(item["symbol"]), str(item["data_type"]),
                str(item["cadence"]), str(item["period"]),
            ),
        ),
        "missing_archives": sorted(missing),
        "datasets": datasets,
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "datasets": len(datasets),
        "archives": len(manifest),
        "missing_archives": len(missing),
        "output": str(args.output),
    }, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
