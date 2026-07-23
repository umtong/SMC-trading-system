from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


def months(start: str, end: str) -> list[str]:
    return pd.period_range(start, end, freq="M").astype(str).tolist()


def get(session: requests.Session, url: str, attempts: int = 5) -> bytes:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=120)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"download failed: {url}: {error}")


def parse_checksum(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="strict").strip()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not match:
        raise ValueError(f"invalid checksum file: {text[:200]}")
    return match.group(1).lower()


def load_month(session: requests.Session, symbol: str, month: str) -> tuple[pd.DataFrame, dict]:
    name = f"{symbol}-15m-{month}.zip"
    url = f"{BASE}/{symbol}/15m/{name}"
    raw = get(session, url)
    expected = parse_checksum(get(session, url + ".CHECKSUM"))
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch {name}: {actual} != {expected}")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [item for item in archive.namelist() if not item.endswith("/")]
        if len(members) != 1:
            raise ValueError(f"unexpected members in {name}: {members}")
        content = archive.read(members[0])
    frame = pd.read_csv(io.BytesIO(content), header=None)
    if frame.shape[1] < 12:
        # Newer archives can contain a header row.
        frame = pd.read_csv(io.BytesIO(content))
        frame.columns = [str(column).strip().lower() for column in frame.columns]
    else:
        frame = frame.iloc[:, :12]
        frame.columns = COLUMNS
    for column in COLUMNS:
        if column not in frame.columns:
            raise ValueError(f"missing {column} in {name}")
    frame = frame[COLUMNS].copy()
    frame["open_time"] = pd.to_datetime(pd.to_numeric(frame["open_time"], errors="raise"), unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(pd.to_numeric(frame["close_time"], errors="raise"), unit="ms", utc=True)
    numeric = [column for column in COLUMNS if column not in {"open_time", "close_time", "ignore"}]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame["symbol"] = symbol
    frame["source_month"] = month
    meta = {
        "symbol": symbol,
        "month": month,
        "url": url,
        "zip_sha256": actual,
        "zip_bytes": len(raw),
        "rows": int(len(frame)),
        "first_open_time": frame["open_time"].min().isoformat(),
        "last_open_time": frame["open_time"].max().isoformat(),
    }
    return frame, meta


def quality(frame: pd.DataFrame) -> dict:
    result: dict[str, object] = {}
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("open_time")
        delta = group["open_time"].diff().dropna()
        gaps = delta[delta != pd.Timedelta(minutes=15)]
        result[symbol] = {
            "rows": int(len(group)),
            "first_open_time": group["open_time"].min().isoformat(),
            "last_open_time": group["open_time"].max().isoformat(),
            "duplicates": int(group["open_time"].duplicated().sum()),
            "non_15m_deltas": int(len(gaps)),
            "largest_delta_minutes": None if delta.empty else float(delta.max().total_seconds() / 60),
            "ohlc_invalid": int(((group["high"] < group[["open", "close"]].max(axis=1)) | (group["low"] > group[["open", "close"]].min(axis=1)) | (group["high"] < group["low"])).sum()),
            "negative_volume": int((group["volume"] < 0).sum()),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--out", type=Path, default=Path("research/data_bundle"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "SMC-ICT-causal-research/1.0"
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    for symbol in args.symbols:
        for month in months(args.start, args.end):
            frame, meta = load_month(session, symbol, month)
            frames.append(frame)
            sources.append(meta)
            print(json.dumps(meta, sort_keys=True), flush=True)
    data = pd.concat(frames, ignore_index=True)
    data = data.sort_values(["symbol", "open_time", "source_month"])
    duplicate_count = int(data.duplicated(["symbol", "open_time"]).sum())
    data = data.drop_duplicates(["symbol", "open_time"], keep="last").reset_index(drop=True)
    data_path = args.out / f"binance_usdm_btc_eth_15m_{args.start}_{args.end}.parquet"
    data.to_parquet(data_path, index=False, compression="zstd")
    payload_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest = {
        "provider": "Binance public data archive",
        "market": "USD-M futures",
        "interval": "15m",
        "start_month": args.start,
        "end_month": args.end,
        "symbols": args.symbols,
        "rows": int(len(data)),
        "duplicates_removed": duplicate_count,
        "parquet": data_path.name,
        "parquet_bytes": data_path.stat().st_size,
        "parquet_sha256": payload_hash,
        "sources": sources,
        "quality": quality(data),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (args.out / "COMPLETE").write_text(payload_hash + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ["rows", "parquet", "parquet_bytes", "parquet_sha256", "quality"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
