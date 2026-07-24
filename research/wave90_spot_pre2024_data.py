from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
START = pd.Timestamp("2021-01-01", tz="UTC")
END = pd.Timestamp("2024-01-01", tz="UTC")
UA = "smc-wave90-spot-pre2024/1.0"
COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
]


def fetch(url: str, attempts: int = 7) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"download failed {url}: {last!r}")


def month_tags() -> list[str]:
    return [str(x) for x in pd.period_range(START, END - pd.Timedelta(days=1), freq="M")]


def decode_epoch(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.dropna().abs()
    median = float(valid.median()) if len(valid) else 0.0
    unit = "ns" if median > 1e17 else "us" if median > 1e14 else "ms" if median > 1e11 else "s"
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def read_archive(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"unexpected members: {members}")
        rows = list(csv.reader(line.decode("utf-8-sig") for line in archive.open(members[0])))
    if rows and rows[0] and rows[0][0].strip().lower().replace(" ", "_") == "open_time":
        rows = rows[1:]
    frame = pd.DataFrame(rows, columns=COLS)
    frame["open_time"] = decode_epoch(frame["open_time"])
    frame["close_time"] = decode_epoch(frame["close_time"])
    for column in COLS[1:6] + COLS[7:11]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["count"] = pd.to_numeric(frame["count"], errors="coerce").astype("Int64")
    return frame.dropna(subset=["open_time", "open", "high", "low", "close"])


def build_symbol(symbol: str, output: Path) -> dict:
    pieces: list[pd.DataFrame] = []
    raw_records: list[dict] = []
    for tag in month_tags():
        name = f"{symbol}-5m-{tag}.zip"
        url = f"{BASE}/{symbol}/5m/{name}"
        checksum_bytes = fetch(url + ".CHECKSUM")
        expected = checksum_bytes.decode("utf-8-sig").strip().split()[0].lower()
        payload = fetch(url)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"checksum mismatch {name}: {actual} != {expected}")
        month = read_archive(payload)
        month = month[(month.open_time >= START) & (month.open_time < END)].copy()
        pieces.append(month)
        raw_records.append({
            "symbol": symbol,
            "month": tag,
            "url": url,
            "sha256": actual,
            "bytes": len(payload),
            "rows": int(len(month)),
        })
        print(json.dumps({"symbol": symbol, "month": tag, "rows": len(month)}), flush=True)

    source = pd.concat(pieces, ignore_index=True).sort_values("open_time", kind="mergesort")
    source = source.drop_duplicates("open_time", keep="last").set_index("open_time")
    exact = pd.date_range(START, END, freq="5min", inclusive="left", tz="UTC")
    source = source.reindex(exact)
    source.index.name = "time"
    source["source_present"] = source[["open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume"]].notna().all(axis=1)
    invalid = source.source_present & (
        (source.high < source[["open", "close", "low"]].max(axis=1))
        | (source.low > source[["open", "close", "high"]].min(axis=1))
        | (source.volume < 0)
        | (source.quote_volume < 0)
        | (source.taker_buy_base_volume < 0)
        | (source.taker_buy_quote_volume < 0)
        | (source.taker_buy_quote_volume > source.quote_volume * 1.000001)
    )
    if bool(invalid.any()):
        raise ValueError(f"invalid OHLCV rows for {symbol}: {int(invalid.sum())}")
    if source.index.has_duplicates or not source.index.is_monotonic_increasing:
        raise ValueError(f"invalid chronology {symbol}")

    keep = [
        "open", "high", "low", "close", "volume", "quote_volume", "count",
        "taker_buy_base_volume", "taker_buy_quote_volume", "close_time", "source_present",
    ]
    normalized = source[keep].reset_index()
    path = output / f"{symbol}_spot_5m_2021_2023.parquet"
    normalized.to_parquet(path, index=False, compression="zstd")
    return {
        "symbol": symbol,
        "rows": int(len(normalized)),
        "present_rows": int(normalized.source_present.sum()),
        "missing_rows": int((~normalized.source_present).sum()),
        "start": normalized.time.min().isoformat(),
        "end": normalized.time.max().isoformat(),
        "file": path.name,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_bytes": path.stat().st_size,
        "raw_files": raw_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = [build_symbol(symbol, args.output) for symbol in SYMBOLS]
    manifest = {
        "study_id": "WAVE90_BINANCE_SPOT_5M_PRE2024_DATA_V1",
        "source": "official Binance Vision spot monthly klines with adjacent SHA-256 CHECKSUM",
        "range": [START.isoformat(), END.isoformat()],
        "2024_or_later_rows_exported": False,
        "strategy_pnl_calculated": False,
        "orders_submitted": False,
        "paper_or_live_started": False,
        "symbols": records,
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes = []
    for path in sorted(args.output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            hashes.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (args.output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
