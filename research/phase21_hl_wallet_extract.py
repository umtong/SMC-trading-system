from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import requests

META_URL = "https://datasets-server.huggingface.co/parquet?dataset=gionuibk%2Fhyperliquid-node-fills-by-block"
BINANCE_ROOT = "https://data.binance.vision/data/futures/um/daily/aggTrades"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--hour", required=True, type=int)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(exist_ok=True)

    day_compact = args.day.replace("-", "")
    src = f"node_fills_by_block/hourly/{day_compact}/{args.hour:02d}.lz4"
    meta = requests.get(META_URL, timeout=120)
    meta.raise_for_status()
    urls = [x["url"] for x in meta.json()["parquet_files"] if x["split"] == "train"]
    if not urls:
        raise RuntimeError("no Hyperliquid parquet shards")

    con = duckdb.connect()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    escaped = "[" + ",".join("'" + u.replace("'", "''") + "'" for u in urls) + "]"
    escaped_src = src.replace("'", "''")
    block_path = out / "hyperliquid_blocks.parquet"
    con.execute(
        f"COPY (SELECT local_time, block_time, block_number, events, _src "
        f"FROM read_parquet({escaped}, union_by_name=true) WHERE _src='{escaped_src}') "
        f"TO '{block_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    blocks = pd.read_parquet(block_path)
    if blocks.empty:
        raise RuntimeError(f"no Hyperliquid blocks for {src}")

    records: list[dict] = []
    for row in blocks.itertuples(index=False):
        events = json.loads(row.events) if isinstance(row.events, str) else row.events
        for item in events or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            wallet, fill = item
            coin = str(fill.get("coin", ""))
            if coin not in {"BTC", "ETH"}:
                continue
            px = float(fill["px"])
            size = float(fill["sz"])
            side = str(fill["side"])
            records.append({
                "wallet": str(wallet).lower(),
                "coin": coin,
                "time_ms": int(fill["time"]),
                "side": side,
                "signed_quote": (1.0 if side == "B" else -1.0) * px * size,
                "quote": px * size,
                "price": px,
                "size": size,
                "crossed": bool(fill.get("crossed", False)),
                "direction": str(fill.get("dir", "")),
                "start_position": float(fill.get("startPosition") or 0.0),
                "closed_pnl": float(fill.get("closedPnl") or 0.0),
                "fee": float(fill.get("fee") or 0.0),
                "trade_id": int(fill.get("tid") or -1),
                "order_id": int(fill.get("oid") or -1),
                "block_number": int(row.block_number),
                "block_time": row.block_time,
                "local_time": row.local_time,
            })
    fills = pd.DataFrame(records)
    if fills.empty:
        raise RuntimeError("no BTC/ETH wallet fills")
    fills = fills.sort_values(["time_ms", "block_number", "wallet", "trade_id"], kind="mergesort")
    fill_path = out / "hyperliquid_wallet_fills.parquet"
    fills.to_parquet(fill_path, index=False, compression="zstd")

    start_ms = int(pd.Timestamp(args.day, tz="UTC").value // 1_000_000) + args.hour * 3_600_000
    end_ms = start_ms + 3_600_000
    binance_parts = []
    sources = {}
    session = requests.Session()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        name = f"{symbol}-aggTrades-{args.day}.zip"
        url = f"{BINANCE_ROOT}/{symbol}/{name}"
        zpath = raw / name
        cpath = raw / f"{name}.CHECKSUM"
        for target, target_url in ((zpath, url), (cpath, url + ".CHECKSUM")):
            response = session.get(target_url, timeout=240)
            response.raise_for_status()
            target.write_bytes(response.content)
        expected = cpath.read_text().split()[0].lower()
        actual = sha256(zpath)
        if actual != expected:
            raise RuntimeError(f"checksum mismatch {name}")
        sources[name] = actual
        with zipfile.ZipFile(zpath) as archive:
            names = [n for n in archive.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise RuntimeError(names)
            payload = archive.read(names[0])
        first = payload.splitlines()[0].decode("utf-8-sig").split(",")[0].strip()
        has_header = not first.lstrip("-").isdigit()
        frame = pd.read_csv(io.BytesIO(payload), header=0 if has_header else None)
        if not has_header:
            frame.columns = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"]
        for col in ("price", "quantity", "transact_time"):
            frame[col] = pd.to_numeric(frame[col], errors="raise")
        if frame.transact_time.median() > 1e14:
            frame["transact_time"] = (frame.transact_time // 1000).astype("int64")
        frame = frame[(frame.transact_time >= start_ms) & (frame.transact_time < end_ms)].copy()
        maker = frame.is_buyer_maker.astype(str).str.lower().isin(["true", "1"])
        frame["symbol"] = symbol
        frame["quote"] = frame.price * frame.quantity
        frame["signed_quote"] = np.where(maker, -frame.quote, frame.quote)
        frame["time_ms"] = frame.transact_time.astype("int64")
        binance_parts.append(frame[["symbol", "time_ms", "price", "quantity", "quote", "signed_quote"]])
    binance = pd.concat(binance_parts, ignore_index=True).sort_values(["time_ms", "symbol"], kind="mergesort")
    if binance.empty:
        raise RuntimeError("no Binance reference trades")
    binance_path = out / "binance_aggtrades.parquet"
    binance.to_parquet(binance_path, index=False, compression="zstd")

    manifest = {
        "day": args.day,
        "hour_utc": args.hour,
        "source_path": src,
        "hyperliquid_block_rows": int(len(blocks)),
        "hyperliquid_fill_rows": int(len(fills)),
        "hyperliquid_wallets": int(fills.wallet.nunique()),
        "binance_trade_rows": int(len(binance)),
        "source_sha256": sources,
        "output_sha256": {p.name: sha256(p) for p in (fill_path, binance_path)},
        "causal_contract": "wallet scores may use only fills and realised closed_pnl timestamped strictly before the decision bucket; Binance entry must be strictly after that completed bucket",
        "orders_submitted": False,
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
