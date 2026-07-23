from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
COLS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def month_keys(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    a = start.tz_convert("UTC").tz_localize(None).to_period("M")
    b = (end.tz_convert("UTC") - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return [str(x) for x in pd.period_range(a, b, freq="M")]


def download_one(cache: Path, symbol: str, month: str) -> dict[str, object]:
    directory = cache / symbol
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-aggTrades-{month}.zip"
    path = directory / name
    checksum_path = directory / f"{name}.CHECKSUM"
    url = f"{BASE}/{symbol}/{name}"
    for target, remote in ((checksum_path, url + ".CHECKSUM"), (path, url)):
        if not target.exists():
            response = requests.get(remote, timeout=120)
            response.raise_for_status()
            target.write_bytes(response.content)
    checksum_text = checksum_path.read_text(encoding="utf-8-sig").strip()
    match = re.search(r"([0-9a-fA-F]{64})", checksum_text)
    if match is None:
        raise ValueError(f"invalid checksum file: {checksum_path}")
    expected = match.group(1).lower()
    actual = sha256_file(path)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch: {path}")
    return {
        "symbol": symbol,
        "month": month,
        "path": str(path),
        "url": url,
        "sha256": actual,
        "bytes": path.stat().st_size,
    }


def download_archives(cache: Path, symbols: list[str], months: list[str], workers: int = 4) -> pd.DataFrame:
    tasks = [(s, m) for s in symbols for m in months]
    rows: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_one, cache, s, m): (s, m) for s, m in tasks}
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
    return pd.DataFrame(rows).sort_values(["symbol", "month"]).reset_index(drop=True)


def _header_mode(handle) -> int | None:
    first = handle.readline()
    handle.seek(0)
    token = first.split(b",", 1)[0].strip().lstrip(b"\xef\xbb\xbf")
    return None if token.isdigit() else 0


def _bool_array(series: pd.Series) -> np.ndarray:
    if series.dtype == bool:
        return series.to_numpy(bool)
    text = series.astype(str).str.strip().str.lower()
    return text.isin(["true", "1", "t"]).to_numpy(bool)


def extract_month(path: Path, symbol: str, start_ms: int, end_ms: int, chunksize: int = 1_000_000) -> pd.DataFrame:
    signal_partials: list[pd.DataFrame] = []
    entry_partials: list[pd.DataFrame] = []
    with zipfile.ZipFile(path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected one CSV member in {path}, got {members}")
        with zf.open(members[0], "r") as raw:
            header = _header_mode(raw)
            reader = pd.read_csv(raw, header=header, names=None if header == 0 else COLS, chunksize=chunksize, low_memory=False)
            for chunk in reader:
                if header == 0:
                    if len(chunk.columns) < 7:
                        raise ValueError(f"unexpected aggTrade schema in {path}: {list(chunk.columns)}")
                    chunk = chunk.iloc[:, :7]
                    chunk.columns = COLS
                ts = pd.to_numeric(chunk["transact_time"], errors="coerce").to_numpy(dtype="float64")
                valid = np.isfinite(ts)
                if not valid.any():
                    continue
                chunk = chunk.loc[valid].copy()
                ts = ts[valid].astype("int64")
                if np.nanmedian(ts) > 10**14:
                    ts = ts // 1000
                in_range = (ts >= start_ms) & (ts < end_ms)
                if not in_range.any():
                    continue
                chunk = chunk.loc[in_range].copy()
                ts = ts[in_range]
                price = pd.to_numeric(chunk["price"], errors="coerce").to_numpy(float)
                qty = pd.to_numeric(chunk["quantity"], errors="coerce").to_numpy(float)
                agg_id = pd.to_numeric(chunk["agg_trade_id"], errors="coerce").fillna(-1).to_numpy("int64")
                maker = _bool_array(chunk["is_buyer_maker"])
                ok = np.isfinite(price) & np.isfinite(qty) & (price > 0) & (qty > 0)
                if not ok.any():
                    continue
                ts, price, qty, agg_id, maker = ts[ok], price[ok], qty[ok], agg_id[ok], maker[ok]
                boundary = ts - (ts % 900_000)
                phase = ts - boundary
                quote = price * qty
                signed_quote = np.where(maker, -quote, quote)
                sig = phase < 10_000
                if sig.any():
                    sdf = pd.DataFrame({
                        "boundary_ms": boundary[sig], "ts": ts[sig], "agg_id": agg_id[sig],
                        "price": price[sig], "quote": quote[sig], "signed_quote": signed_quote[sig],
                    }).sort_values(["boundary_ms", "ts", "agg_id"], kind="mergesort")
                    signal_partials.append(sdf.groupby("boundary_ms", sort=False).agg(
                        first_ts=("ts", "first"), first_agg_id=("agg_id", "first"), first_price=("price", "first"),
                        last_ts=("ts", "last"), last_agg_id=("agg_id", "last"), last_price=("price", "last"),
                        signal_quote=("quote", "sum"), signal_signed_quote=("signed_quote", "sum"),
                        signal_agg_count=("price", "size"),
                    ).reset_index())
                ent = (phase >= 10_000) & (phase < 60_000)
                if ent.any():
                    edf = pd.DataFrame({
                        "boundary_ms": boundary[ent], "entry_ts": ts[ent],
                        "entry_agg_id": agg_id[ent], "entry_price": price[ent],
                    }).sort_values(["boundary_ms", "entry_ts", "entry_agg_id"], kind="mergesort")
                    entry_partials.append(edf.groupby("boundary_ms", sort=False).first().reset_index())
    if not signal_partials:
        return pd.DataFrame()
    sig = pd.concat(signal_partials, ignore_index=True).sort_values(["boundary_ms", "first_ts", "first_agg_id"], kind="mergesort")
    sig = sig.groupby("boundary_ms", sort=False).agg(
        first_ts=("first_ts", "first"), first_agg_id=("first_agg_id", "first"), first_price=("first_price", "first"),
        last_ts=("last_ts", "last"), last_agg_id=("last_agg_id", "last"), last_price=("last_price", "last"),
        signal_quote=("signal_quote", "sum"), signal_signed_quote=("signal_signed_quote", "sum"),
        signal_agg_count=("signal_agg_count", "sum"),
    ).reset_index()
    if entry_partials:
        ent = pd.concat(entry_partials, ignore_index=True).sort_values(["boundary_ms", "entry_ts", "entry_agg_id"], kind="mergesort").groupby("boundary_ms", sort=False).first().reset_index()
        sig = sig.merge(ent, on="boundary_ms", how="left", validate="one_to_one")
    else:
        sig[["entry_ts", "entry_agg_id", "entry_price"]] = np.nan
    sig["symbol"] = symbol
    return sig


def add_causal_features(frame: pd.DataFrame, prior_events: int = 1920, minimum_prior: int = 960) -> pd.DataFrame:
    out = frame.sort_values("boundary_ms", kind="mergesort").reset_index(drop=True).copy()
    out["boundary_time"] = pd.to_datetime(out.boundary_ms, unit="ms", utc=True)
    out["entry_time"] = pd.to_datetime(out.entry_ts, unit="ms", utc=True)
    out["imbalance"] = out.signal_signed_quote / out.signal_quote
    out["opening_return"] = np.log(out.last_price / out.first_price)
    out["log_signal_quote"] = np.log1p(out.signal_quote)
    prior = out.log_signal_quote.shift(1)
    mean = prior.rolling(prior_events, min_periods=minimum_prior).mean()
    std = prior.rolling(prior_events, min_periods=minimum_prior).std(ddof=0)
    out["volume_z"] = (out.log_signal_quote - mean) / std.replace(0, np.nan)
    out["clock_slot"] = out.boundary_time.dt.hour * 4 + out.boundary_time.dt.minute // 15
    out["clock_volume_z"] = np.nan
    for _, idx in out.groupby("clock_slot", sort=False).groups.items():
        values = out.loc[idx, "log_signal_quote"]
        p = values.shift(1)
        m = p.rolling(60, min_periods=20).mean()
        s = p.rolling(60, min_periods=20).std(ddof=0)
        out.loc[idx, "clock_volume_z"] = ((values - m) / s.replace(0, np.nan)).to_numpy()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    args.output.mkdir(parents=True, exist_ok=True)
    months = month_keys(start, end)
    manifest = download_archives(args.cache, args.symbols, months, args.workers)
    manifest.to_csv(args.output / "input_manifest.csv", index=False)
    summaries = []
    for symbol in args.symbols:
        pieces = []
        for month in months:
            path = args.cache / symbol / f"{symbol}-aggTrades-{month}.zip"
            part = extract_month(path, symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
            if not part.empty:
                pieces.append(part)
        if not pieces:
            raise ValueError(f"no quarter-hour events for {symbol}")
        frame = add_causal_features(pd.concat(pieces, ignore_index=True))
        path = args.output / f"{symbol}_quarter_hour_10s.csv.gz"
        frame.to_csv(path, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
        summaries.append({
            "symbol": symbol, "events": len(frame),
            "first_boundary": frame.boundary_time.min().isoformat(),
            "last_boundary": frame.boundary_time.max().isoformat(),
            "missing_entry": int(frame.entry_price.isna().sum()),
            "sha256": sha256_file(path), "bytes": path.stat().st_size,
        })
    (args.output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
