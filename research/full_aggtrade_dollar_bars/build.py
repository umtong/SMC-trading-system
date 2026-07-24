from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly"
CONFIGS = ((72, 600), (144, 600), (288, 600), (288, 300))
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
AGG_COLUMNS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def infer_unit(values: pd.Series) -> str:
    med = float(pd.to_numeric(values, errors="raise").abs().median())
    if med > 1e17:
        return "ns"
    if med > 1e14:
        return "us"
    if med > 1e11:
        return "ms"
    return "s"


def download_verified(session: requests.Session, url: str) -> tuple[bytes, dict]:
    cr = session.get(url + ".CHECKSUM", timeout=(30, 180))
    cr.raise_for_status()
    expected = cr.text.strip().split()[0].lower()
    ar = session.get(url, timeout=(30, 600))
    ar.raise_for_status()
    payload = ar.content
    actual = sha256(payload)
    if actual != expected:
        raise RuntimeError(f"checksum mismatch {url}: expected={expected} actual={actual}")
    return payload, {"url": url, "bytes": len(payload), "sha256": actual}


def parse_kline(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"expected one CSV, got {names}")
        raw = zf.read(names[0])
    first = raw.splitlines()[0].decode("utf-8", "replace").split(",")[0].strip().lower()
    header = first in {"open_time", "open time"}
    df = pd.read_csv(io.BytesIO(raw), header=0 if header else None, names=None if header else KLINE_COLUMNS)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    aliases = {
        "quote_asset_volume": "quote_volume",
        "number_of_trades": "num_trades",
        "taker_buy_base_asset_volume": "taker_buy_base",
        "taker_buy_quote_asset_volume": "taker_buy_quote",
    }
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
    if "open_time" not in df.columns:
        df.columns = KLINE_COLUMNS[: len(df.columns)]
    unit = infer_unit(df["open_time"])
    ts = pd.to_datetime(pd.to_numeric(df["open_time"], errors="raise"), unit=unit, utc=True)
    return pd.DataFrame({"timestamp": ts, "quote_volume": pd.to_numeric(df["quote_volume"], errors="raise")})


def agg_second_chunks(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"expected one CSV, got {names}")
        with zf.open(names[0]) as stream:
            first = stream.readline().decode("utf-8", "replace")
            stream.seek(0)
            first_col = first.split(",")[0].strip().lower()
            header = first_col in {"agg_trade_id", "aggregate_trade_id", "a"}
            reader = pd.read_csv(stream, header=0 if header else None, names=None if header else AGG_COLUMNS, chunksize=1_000_000)
            carry = None
            for df in reader:
                df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
                aliases = {
                    "a": "agg_trade_id", "p": "price", "q": "quantity", "f": "first_trade_id",
                    "l": "last_trade_id", "t": "transact_time", "m": "is_buyer_maker",
                    "aggregate_trade_id": "agg_trade_id", "transact_time_ms": "transact_time",
                }
                df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
                if "transact_time" not in df.columns:
                    df.columns = AGG_COLUMNS[: len(df.columns)]
                tnum = pd.to_numeric(df["transact_time"], errors="raise")
                unit = infer_unit(tnum)
                ns = pd.to_datetime(tnum, unit=unit, utc=True).astype("datetime64[ns, UTC]").astype("int64").to_numpy()
                sec = ns // 1_000_000_000
                price = pd.to_numeric(df["price"], errors="raise").to_numpy(float)
                qty = pd.to_numeric(df["quantity"], errors="raise").to_numpy(float)
                qv = price * qty
                maker = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1", "t"]).to_numpy()
                signed = np.where(maker, -qv, qv)
                first_id = pd.to_numeric(df["first_trade_id"], errors="coerce").to_numpy(float)
                last_id = pd.to_numeric(df["last_trade_id"], errors="coerce").to_numpy(float)
                raw_count = np.where(np.isfinite(first_id) & np.isfinite(last_id), last_id - first_id + 1, 1.0)
                z = pd.DataFrame({"sec": sec, "price": price, "qv": qv, "signed": signed, "raw_count": raw_count})
                if carry is not None:
                    z = pd.concat([carry, z], ignore_index=True)
                g = z.groupby("sec", sort=True).agg(
                    open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"),
                    quote_volume=("qv", "sum"), signed_quote=("signed", "sum"), agg_count=("price", "size"), raw_count=("raw_count", "sum"),
                ).reset_index()
                # Keep the final second as carry because it may continue in the next CSV chunk.
                if len(g) > 1:
                    last_sec = int(g.sec.iloc[-1])
                    yield g.iloc[:-1].copy()
                    carry = z[z.sec == last_sec].copy()
                else:
                    carry = z
            if carry is not None and len(carry):
                g = carry.groupby("sec", sort=True).agg(
                    open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"),
                    quote_volume=("qv", "sum"), signed_quote=("signed", "sum"), agg_count=("price", "size"), raw_count=("raw_count", "sum"),
                ).reset_index()
                yield g


@dataclass
class BarState:
    bars_per_day: int
    cap_seconds: int
    rows: list[dict] = field(default_factory=list)
    active: bool = False
    start_sec: int = 0
    day: pd.Timestamp | None = None
    open: float = np.nan
    high: float = -np.inf
    low: float = np.inf
    close: float = np.nan
    qv: float = 0.0
    signed: float = 0.0
    agg_count: float = 0.0
    raw_count: float = 0.0
    pending_index: int | None = None

    def fill_pending(self, sec: int, price: float) -> None:
        if self.pending_index is not None:
            self.rows[self.pending_index]["entry_time"] = pd.Timestamp(sec, unit="s", tz="UTC")
            self.rows[self.pending_index]["entry_price"] = float(price)
            self.pending_index = None

    def reset(self) -> None:
        self.active = False
        self.qv = self.signed = self.agg_count = self.raw_count = 0.0
        self.high = -np.inf
        self.low = np.inf
        self.open = self.close = np.nan

    def close_bar(self, decision_sec: int, reason: int) -> None:
        if not self.active:
            return
        row = {
            "start_time": pd.Timestamp(self.start_sec, unit="s", tz="UTC"),
            "decision_time": pd.Timestamp(decision_sec, unit="s", tz="UTC"),
            "entry_time": pd.NaT,
            "entry_price": np.nan,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "quote_volume": self.qv, "signed_quote": self.signed,
            "imbalance": self.signed / self.qv if self.qv > 0 else np.nan,
            "agg_count": self.agg_count, "raw_count": self.raw_count,
            "duration_seconds": decision_sec - self.start_sec,
            "close_reason": reason,
            "bars_per_day_target": self.bars_per_day,
            "clock_cap_seconds": self.cap_seconds,
        }
        self.rows.append(row)
        self.pending_index = len(self.rows) - 1
        self.reset()

    def process(self, sec: int, op: float, hi: float, lo: float, cl: float, qv: float, signed: float, ac: float, rc: float, threshold: float) -> None:
        self.fill_pending(sec, op)
        current_day = pd.Timestamp(sec, unit="s", tz="UTC").floor("D")
        if self.active and self.day is not None and current_day != self.day:
            self.close_bar(int(current_day.timestamp()), 0)
            self.fill_pending(sec, op)
        if self.active and sec >= self.start_sec + self.cap_seconds:
            self.close_bar(self.start_sec + self.cap_seconds, 2)
            self.fill_pending(sec, op)
        if not self.active:
            self.active = True
            self.start_sec = sec
            self.day = current_day
            self.open = float(op)
        self.high = max(self.high, float(hi))
        self.low = min(self.low, float(lo))
        self.close = float(cl)
        self.qv += float(qv)
        self.signed += float(signed)
        self.agg_count += float(ac)
        self.raw_count += float(rc)
        if np.isfinite(threshold) and self.qv >= threshold:
            # The full second is observed before the signal is emitted.
            self.close_bar(sec + 1, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    symbol = args.symbol.upper()
    year = args.year
    last_month = 6 if year == 2025 else 12
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "SMC-full-aggtrade-dollar-bars/1.0"

    # Klines supply causal completed-day dollar-volume thresholds.
    kline_records = []
    daily_parts = []
    periods = [pd.Period(f"{year-1}-12", freq="M")] + list(pd.period_range(f"{year}-01", f"{year}-{last_month:02d}", freq="M"))
    for period in periods:
        stamp = str(period)
        name = f"{symbol}-1m-{stamp}.zip"
        url = f"{BASE}/klines/{symbol}/1m/{name}"
        payload, rec = download_verified(session, url)
        rec["period"] = stamp
        kline_records.append(rec)
        k = parse_kline(payload)
        daily_parts.append(k.set_index("timestamp").quote_volume.resample("1D").sum(min_count=1))
        print("KLINE", symbol, stamp, rec["bytes"], rec["sha256"], flush=True)
    daily = pd.concat(daily_parts).groupby(level=0).sum().sort_index()
    trailing = daily.shift(1).rolling(20, min_periods=10).median()
    threshold_maps = {bpd: trailing / bpd for bpd, _ in CONFIGS}

    states = {(bpd, cap): BarState(bpd, cap) for bpd, cap in CONFIGS}
    agg_records = []
    for month in range(1, last_month + 1):
        stamp = f"{year}-{month:02d}"
        name = f"{symbol}-aggTrades-{stamp}.zip"
        url = f"{BASE}/aggTrades/{symbol}/{name}"
        payload, rec = download_verified(session, url)
        rec["period"] = stamp
        agg_records.append(rec)
        seconds = 0
        for g in agg_second_chunks(payload):
            seconds += len(g)
            sec_arr = g.sec.to_numpy(np.int64)
            for i in range(len(g)):
                sec = int(sec_arr[i])
                day = pd.Timestamp(sec, unit="s", tz="UTC").floor("D")
                for (bpd, cap), state in states.items():
                    threshold = float(threshold_maps[bpd].get(day, np.nan))
                    state.process(
                        sec,
                        float(g.open.iat[i]), float(g.high.iat[i]), float(g.low.iat[i]), float(g.close.iat[i]),
                        float(g.quote_volume.iat[i]), float(g.signed_quote.iat[i]), float(g.agg_count.iat[i]), float(g.raw_count.iat[i]),
                        threshold,
                    )
        print("AGG", symbol, stamp, rec["bytes"], rec["sha256"], "seconds", seconds, flush=True)

    end_sec = int(pd.Timestamp(f"{year + 1}-01-01", tz="UTC").timestamp()) if year < 2025 else int(pd.Timestamp("2025-07-01", tz="UTC").timestamp())
    outputs = []
    for (bpd, cap), state in states.items():
        if state.active:
            state.close_bar(end_sec, 0)
        df = pd.DataFrame(state.rows)
        df["symbol"] = symbol
        df = df[(df.start_time >= pd.Timestamp(f"{year}-01-01", tz="UTC")) & (df.start_time < pd.Timestamp(end_sec, unit="s", tz="UTC"))].copy()
        path = out / f"{symbol}_{year}_bpd{bpd}_cap{cap}s.parquet"
        df.to_parquet(path, index=False, compression="zstd")
        outputs.append({
            "bars_per_day": bpd, "cap_seconds": cap, "rows": len(df),
            "threshold_rows": int((df.close_reason == 1).sum()),
            "clock_rows": int((df.close_reason == 2).sum()),
            "residual_rows": int((df.close_reason == 0).sum()),
            "entry_missing": int(df.entry_price.isna().sum()),
            "path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    manifest = {
        "symbol": symbol, "year": year, "end_exclusive": pd.Timestamp(end_sec, unit="s", tz="UTC").isoformat(),
        "threshold_rule": "20 completed UTC-day median quote volume divided by target bars/day",
        "entry_rule": "first observed aggregate-trade second after decision; full threshold-crossing second is observed",
        "kline_archives": kline_records, "aggtrade_archives": agg_records, "outputs": outputs,
    }
    (out / f"{symbol}_{year}_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"symbol": symbol, "year": year, "outputs": outputs}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
