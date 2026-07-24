from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ENDPOINTS = (
    "https://api.bybit.com",
    "https://api.bytick.com",
)
KLINE_PATH = "/v5/market/kline"
FUNDING_PATH = "/v5/market/funding/history"


def utc_ms(value: str) -> int:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return int(stamp.timestamp() * 1000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(
    session: requests.Session,
    path: str,
    params: dict[str, Any],
    *,
    retries: int = 8,
) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        for base in ENDPOINTS:
            try:
                response = session.get(base + path, params=params, timeout=45)
                if response.status_code in {403, 429}:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                response.raise_for_status()
                payload = response.json()
                if int(payload.get("retCode", -1)) != 0:
                    raise RuntimeError(
                        f"Bybit retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}"
                    )
                return payload
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(min(0.5 * 2 ** (attempt - 1), 20.0))
    assert last is not None
    raise last


def fetch_klines(
    session: requests.Session,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    pause: float,
) -> pd.DataFrame:
    rows: dict[int, list[str]] = {}
    cursor = end_ms
    calls = 0
    while cursor >= start_ms:
        payload = request_json(
            session,
            KLINE_PATH,
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "1",
                "end": cursor,
                "limit": 1000,
            },
        )
        batch = payload.get("result", {}).get("list", [])
        if not batch:
            break
        starts = []
        for item in batch:
            if len(item) < 7:
                raise ValueError(f"unexpected kline row for {symbol}: {item!r}")
            stamp = int(item[0])
            starts.append(stamp)
            if start_ms <= stamp <= end_ms:
                rows[stamp] = list(item[:7])
        oldest = min(starts)
        calls += 1
        if calls % 250 == 0:
            print(
                json.dumps(
                    {
                        "symbol": symbol,
                        "kind": "kline",
                        "calls": calls,
                        "rows": len(rows),
                        "oldest": pd.to_datetime(oldest, unit="ms", utc=True).isoformat(),
                    }
                ),
                flush=True,
            )
        if oldest <= start_ms or oldest >= cursor:
            break
        cursor = oldest - 1
        if pause:
            time.sleep(pause)
    frame = pd.DataFrame(
        [rows[key] for key in sorted(rows)],
        columns=[
            "open_time_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
        ],
    )
    if frame.empty:
        raise RuntimeError(f"no Bybit kline rows returned for {symbol}")
    frame["open_time_ms"] = pd.to_numeric(frame["open_time_ms"], errors="raise").astype("int64")
    frame["open_time"] = pd.to_datetime(frame["open_time_ms"], unit="ms", utc=True)
    for column in ("open", "high", "low", "close", "volume", "turnover"):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame["symbol"] = symbol
    return frame[
        ["open_time", "open", "high", "low", "close", "volume", "turnover", "symbol"]
    ]


def fetch_funding(
    session: requests.Session,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    pause: float,
) -> pd.DataFrame:
    rows: dict[int, dict[str, Any]] = {}
    cursor = end_ms
    calls = 0
    while cursor >= start_ms:
        payload = request_json(
            session,
            FUNDING_PATH,
            {
                "category": "linear",
                "symbol": symbol,
                "endTime": cursor,
                "limit": 200,
            },
        )
        batch = payload.get("result", {}).get("list", [])
        if not batch:
            break
        stamps = []
        for item in batch:
            stamp = int(item["fundingRateTimestamp"])
            stamps.append(stamp)
            if start_ms <= stamp <= end_ms:
                rows[stamp] = item
        oldest = min(stamps)
        calls += 1
        if oldest <= start_ms or oldest >= cursor:
            break
        cursor = oldest - 1
        if pause:
            time.sleep(pause)
    frame = pd.DataFrame(
        {
            "funding_time": pd.to_datetime(sorted(rows), unit="ms", utc=True),
            "funding_rate": [float(rows[key]["fundingRate"]) for key in sorted(rows)],
            "symbol": symbol,
        }
    )
    if frame.empty:
        raise RuntimeError(f"no Bybit funding rows returned for {symbol}")
    return frame


def validate_klines(frame: pd.DataFrame) -> dict[str, Any]:
    ordered = frame.sort_values("open_time").reset_index(drop=True)
    duplicates = int(ordered["open_time"].duplicated().sum())
    delta = ordered["open_time"].diff().dropna()
    irregular = delta[delta != pd.Timedelta(minutes=1)]
    bad = (
        (ordered["high"] < ordered[["open", "low", "close"]].max(axis=1))
        | (ordered["low"] > ordered[["open", "high", "close"]].min(axis=1))
        | (ordered[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (ordered[["volume", "turnover"]] < 0).any(axis=1)
    )
    if duplicates or int(bad.sum()):
        raise ValueError(f"kline validation failed: duplicates={duplicates}, bad={int(bad.sum())}")
    samples = []
    for index in irregular.index[:50]:
        samples.append(
            {
                "previous": ordered.iloc[index - 1]["open_time"].isoformat(),
                "next": ordered.iloc[index]["open_time"].isoformat(),
                "seconds": float(delta.loc[index].total_seconds()),
            }
        )
    return {
        "rows": int(len(ordered)),
        "start": ordered["open_time"].min().isoformat(),
        "end": ordered["open_time"].max().isoformat(),
        "duplicates": duplicates,
        "irregular_intervals": int(len(irregular)),
        "gap_sample": samples,
    }


def write_csv(frame: pd.DataFrame, path: Path) -> dict[str, Any]:
    frame.to_csv(
        path,
        index=False,
        compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
        float_format="%.12g",
    )
    return {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "rows": int(len(frame)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--start", default="2020-04-01T00:00:00Z")
    parser.add_argument("--end", default="2026-07-21T23:59:00Z")
    parser.add_argument("--pause", type=float, default=0.04)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    start_ms = utc_ms(args.start)
    end_ms = utc_ms(args.end)
    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "smc-ict-crossvenue-research/1.0"})

    datasets: dict[str, dict[str, Any]] = {}
    for symbol in args.symbols:
        klines = fetch_klines(session, symbol, start_ms, end_ms, pause=args.pause)
        kline_path = args.output / f"{symbol}_linear_1m.csv.gz"
        kline_info = write_csv(klines, kline_path)
        kline_info.update(validate_klines(klines))
        datasets[f"{symbol}_linear_1m"] = kline_info

        funding = fetch_funding(session, symbol, start_ms, end_ms, pause=args.pause)
        funding_path = args.output / f"{symbol}_funding.csv.gz"
        funding_info = write_csv(funding, funding_path)
        funding_info.update(
            {
                "start": funding["funding_time"].min().isoformat(),
                "end": funding["funding_time"].max().isoformat(),
            }
        )
        datasets[f"{symbol}_funding"] = funding_info

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "Bybit V5 public market API",
        "endpoints": list(ENDPOINTS),
        "symbols": args.symbols,
        "requested_start": pd.to_datetime(start_ms, unit="ms", utc=True).isoformat(),
        "requested_end": pd.to_datetime(end_ms, unit="ms", utc=True).isoformat(),
        "availability_contract": "closed 1m bars only; live use requires websocket confirm=true",
        "datasets": datasets,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
