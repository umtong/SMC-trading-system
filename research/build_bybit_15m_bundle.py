from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import requests

URL = "https://api.bybit.com/v5/market/kline"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def request(session: requests.Session, params: dict, attempts: int = 8) -> dict:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(payload)
            return payload
        except Exception as exc:
            error = exc
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Bybit request failed: {params}: {error}")


def fetch_symbol(session: requests.Session, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    start_ms = int(start.timestamp() * 1000)
    cursor_end = int(end.timestamp() * 1000) - 1
    rows: dict[int, list[str]] = {}
    while cursor_end >= start_ms:
        payload = request(session, {
            "category": "linear",
            "symbol": symbol,
            "interval": "15",
            "start": start_ms,
            "end": cursor_end,
            "limit": 1000,
        })
        items = payload.get("result", {}).get("list", [])
        if not items:
            break
        oldest = cursor_end
        for item in items:
            timestamp = int(item[0])
            if start_ms <= timestamp < int(end.timestamp() * 1000):
                rows[timestamp] = item
            oldest = min(oldest, timestamp)
        print(json.dumps({"symbol": symbol, "cursor_end": cursor_end, "returned": len(items), "oldest": oldest, "accumulated": len(rows)}), flush=True)
        if oldest <= start_ms or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.06)
    if not rows:
        raise RuntimeError(f"no rows for {symbol}")
    frame = pd.DataFrame([rows[key] for key in sorted(rows)], columns=["open_time", "open", "high", "low", "close", "volume", "turnover"])
    frame["open_time"] = pd.to_datetime(pd.to_numeric(frame["open_time"], errors="raise"), unit="ms", utc=True)
    for column in ["open", "high", "low", "close", "volume", "turnover"]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame["symbol"] = symbol
    return frame


def quality(frame: pd.DataFrame) -> dict:
    output = {}
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("open_time")
        delta = group.open_time.diff().dropna()
        output[symbol] = {
            "rows": int(len(group)),
            "first": group.open_time.min().isoformat(),
            "last": group.open_time.max().isoformat(),
            "duplicates": int(group.open_time.duplicated().sum()),
            "non_15m_deltas": int((delta != pd.Timedelta(minutes=15)).sum()),
            "largest_delta_minutes": None if delta.empty else float(delta.max().total_seconds() / 60),
            "ohlc_invalid": int(((group.high < group[["open", "close"]].max(axis=1)) | (group.low > group[["open", "close"]].min(axis=1)) | (group.high < group.low)).sum()),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-07-24")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--out", type=Path, default=Path("research/bybit_data_bundle"))
    args = parser.parse_args()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    args.out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "SMC-ICT-causal-external-validation/1.0"
    frames = [fetch_symbol(session, symbol, start, end) for symbol in args.symbols]
    data = pd.concat(frames, ignore_index=True).sort_values(["symbol", "open_time"])
    data = data.drop_duplicates(["symbol", "open_time"], keep="last").reset_index(drop=True)
    outputs = []
    for year, frame in data.groupby(data.open_time.dt.year, sort=True):
        path = args.out / f"bybit_linear_btc_eth_15m_{year}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        outputs.append({
            "year": int(year),
            "file": path.name,
            "rows": int(len(frame)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "first": frame.open_time.min().isoformat(),
            "last": frame.open_time.max().isoformat(),
        })
    manifest = {
        "provider": "Bybit V5 public market API",
        "endpoint": URL,
        "category": "linear",
        "interval": "15",
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "symbols": args.symbols,
        "outputs": outputs,
        "quality": quality(data),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    complete = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
    (args.out / "COMPLETE").write_text(complete + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
