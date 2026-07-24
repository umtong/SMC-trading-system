from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request
import zipfile

import pandas as pd

TRADED = ("BTCUSDT", "ETHUSDT")
CONTEXT = ("SOLUSDT", "XRPUSDT")
MONTHS = tuple([f"2025-{month:02d}" for month in range(1, 13)] + ["2026-01"])
CANDIDATE_ID = "v25q_btc_anchor_weekly_monthly_phase_v1"
EVALUATOR_SHA256 = "50b3433be4279bf638fb8be4db1cd25627ca103b4acb00a6d60c9e0478d47a7d"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_bytes(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(7):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "v25q-frozen-public/1.0"})
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except Exception as exc:
            last = exc
            time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"{url}: {last!r}")


def identity(market: str, kind: str, symbol: str, month: str, cache: Path) -> tuple[Path, str]:
    name = f"{symbol}-1m-{month}.zip"
    if market == "spot":
        url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{name}"
        folder = cache / "spot" / symbol
    else:
        url = f"https://data.binance.vision/data/futures/um/monthly/{kind}/{symbol}/1m/{name}"
        folder = cache / "futures" / kind / symbol
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name, url


def ensure(job: tuple[str, str, str, str], cache: Path) -> dict:
    market, kind, symbol, month = job
    path, url = identity(market, kind, symbol, month, cache)
    checksum = path.with_name(path.name + ".CHECKSUM")
    if not checksum.exists():
        checksum.write_bytes(get_bytes(url + ".CHECKSUM"))
    match = re.search(rb"([0-9a-fA-F]{64})", checksum.read_bytes())
    if not match:
        raise ValueError(f"bad checksum document: {checksum}")
    expected = match.group(1).decode().lower()
    if not path.exists() or sha256_file(path) != expected:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(get_bytes(url))
        observed = sha256_file(temporary)
        if observed != expected:
            raise ValueError(f"checksum mismatch {url}: {observed} != {expected}")
        temporary.replace(path)
    return {
        "market": market,
        "kind": kind,
        "symbol": symbol,
        "month": month,
        "url": url,
        "sha256": expected,
        "bytes": path.stat().st_size,
        "path": str(path),
    }


def read_kline(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise ValueError(f"unexpected ZIP members: {path}: {members}")
        raw = pd.read_csv(archive.open(members[0]), header=None)
    if raw.shape[1] < 11:
        raise ValueError(f"bad kline schema: {path}: {raw.shape}")
    frame = raw.iloc[:, [0, 1, 2, 3, 4, 7, 8, 10]].copy()
    frame.columns = ["raw_time", "open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote"]
    timestamps = pd.to_numeric(frame.raw_time, errors="raise").astype("int64")
    unit = "us" if float(timestamps.median()) > 1e14 else "ms"
    frame["open_time"] = pd.to_datetime(timestamps, unit=unit, utc=True)
    for column in ["open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote"]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    return frame[["open_time", "open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote"]]


def write_segmented(frame: pd.DataFrame, output: Path, symbol: str, kind: str) -> dict:
    frame = frame.sort_values("open_time", kind="mergesort").reset_index(drop=True)
    if frame.open_time.duplicated().any():
        raise ValueError(f"duplicate minute: {symbol} {kind}")
    differences = frame.open_time.diff()
    frame["segment_id"] = (differences.isna() | differences.ne(pd.Timedelta(minutes=1))).cumsum().astype("int64") - 1
    path = output / f"{symbol}_{kind}_1m.csv.gz"
    frame.to_csv(path, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
    return {
        "symbol": symbol,
        "kind": kind,
        "rows": len(frame),
        "segments": int(frame.segment_id.nunique()),
        "first": str(frame.open_time.min()),
        "last": str(frame.open_time.max()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def run(cache: Path, output: Path, workers: int) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    jobs: list[tuple[str, str, str, str]] = []
    for symbol in TRADED:
        for month in MONTHS:
            jobs.extend((("spot", "klines", symbol, month), ("futures", "klines", symbol, month), ("futures", "markPriceKlines", symbol, month)))
    for symbol in CONTEXT:
        for month in MONTHS:
            jobs.append(("futures", "klines", symbol, month))
    records = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(ensure, job, cache) for job in jobs]
        for count, future in enumerate(as_completed(futures), 1):
            records.append(future.result())
            if count % 20 == 0 or count == len(futures):
                print(f"downloaded={count}/{len(futures)}", flush=True)
    pd.DataFrame(records).sort_values(["symbol", "month", "market", "kind"]).to_csv(output / "input_manifest.csv", index=False)

    summaries = []
    for symbol in TRADED:
        spot_parts, futures_parts = [], []
        for month in MONTHS:
            spot_path, _ = identity("spot", "klines", symbol, month, cache)
            futures_path, _ = identity("futures", "klines", symbol, month, cache)
            mark_path, _ = identity("futures", "markPriceKlines", symbol, month, cache)
            spot_parts.append(read_kline(spot_path))
            contract = read_kline(futures_path)
            mark = read_kline(mark_path)[["open_time", "close"]].rename(columns={"close": "mark_close"})
            joined = contract.merge(mark, on="open_time", how="left", validate="one_to_one")
            joined = joined[joined.mark_close.notna()].copy()
            if joined.empty:
                raise ValueError(f"no contract/mark overlap: {symbol} {month}")
            futures_parts.append(joined)
        summaries.append(write_segmented(pd.concat(spot_parts, ignore_index=True), output, symbol, "spot"))
        summaries.append(write_segmented(pd.concat(futures_parts, ignore_index=True), output, symbol, "fut"))
    for symbol in CONTEXT:
        parts = []
        for month in MONTHS:
            path, _ = identity("futures", "klines", symbol, month, cache)
            parts.append(read_kline(path))
        summaries.append(write_segmented(pd.concat(parts, ignore_index=True), output, symbol, "fut"))

    result = {
        "candidate_id": CANDIDATE_ID,
        "evaluator_sha256": EVALUATOR_SHA256,
        "period": ["2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        "warmout_through": "2026-01-31T23:59:00Z",
        "summaries": summaries,
        "paper_or_live_authority": False,
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.cache, args.output, args.workers), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
