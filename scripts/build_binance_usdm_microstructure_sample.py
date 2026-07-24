from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://data.binance.vision/data/futures/um/daily"
BOOK_COLUMNS = [
    "update_id",
    "best_bid_price",
    "best_bid_qty",
    "best_ask_price",
    "best_ask_qty",
    "transaction_time",
    "event_time",
]
AGG_COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]


@dataclass(frozen=True)
class ArchiveSpec:
    symbol: str
    data_type: str
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.data_type}-{self.day}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.data_type}/{self.symbol}/{self.filename}"


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
        raise ValueError(f"invalid checksum response: {text[:160]!r}")
    return match.group(1).lower()


def fetch_verified(
    spec: ArchiveSpec,
    *,
    timeout: int = 180,
    retries: int = 5,
) -> tuple[bytes, dict[str, object]]:
    headers = {"User-Agent": "smc-ict-microstructure-research/1.0"}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            checksum_response = requests.get(
                spec.url + ".CHECKSUM", headers=headers, timeout=timeout
            )
            checksum_response.raise_for_status()
            expected = parse_checksum(checksum_response.text)
            actual = sha256_bytes(blob)
            if actual != expected:
                raise ValueError(
                    f"checksum mismatch for {spec.filename}: {actual} != {expected}"
                )
            return blob, {
                **asdict(spec),
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
            time.sleep(min(2**attempt, 20))
    if last_error is None:
        raise RuntimeError("retry loop ended without an exception")
    raise last_error


def _member_name(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected exactly one CSV member, found {names}")
        return names[0]


def _has_header(blob: bytes, member: str) -> bool:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        with archive.open(member) as handle:
            first = handle.readline().decode("utf-8", errors="replace").strip().lower()
    return any(ch.isalpha() for ch in first)


def iter_csv_chunks(
    blob: bytes,
    *,
    columns: list[str],
    chunksize: int = 750_000,
) -> Iterator[pd.DataFrame]:
    member = _member_name(blob)
    header = 0 if _has_header(blob, member) else None
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        with archive.open(member) as handle:
            reader = pd.read_csv(handle, header=header, chunksize=chunksize)
            for chunk in reader:
                if len(chunk.columns) < len(columns):
                    raise ValueError(
                        f"{member}: expected at least {len(columns)} columns, got {len(chunk.columns)}"
                    )
                chunk = chunk.iloc[:, : len(columns)].copy()
                chunk.columns = columns
                yield chunk


def timestamp_ms(values: pd.Series) -> np.ndarray:
    numeric = pd.to_numeric(values, errors="raise").to_numpy(dtype=np.float64)
    finite = np.abs(numeric[np.isfinite(numeric)])
    if finite.size == 0:
        raise ValueError("timestamp column is empty")
    median = float(np.median(finite))
    divisor = 1000.0 if median > 1e14 else 1.0
    return np.rint(numeric / divisor).astype(np.int64)


def process_book(blob: bytes, bucket_ms: int) -> tuple[pd.DataFrame, int]:
    pieces: list[pd.DataFrame] = []
    rows = 0
    previous: tuple[float, float, float, float] | None = None
    for chunk in iter_csv_chunks(blob, columns=BOOK_COLUMNS):
        rows += len(chunk)
        for column in (
            "best_bid_price",
            "best_bid_qty",
            "best_ask_price",
            "best_ask_qty",
        ):
            chunk[column] = pd.to_numeric(chunk[column], errors="coerce")
        times = timestamp_ms(chunk["transaction_time"])
        bid = chunk["best_bid_price"].to_numpy(dtype=np.float64)
        bqty = chunk["best_bid_qty"].to_numpy(dtype=np.float64)
        ask = chunk["best_ask_price"].to_numpy(dtype=np.float64)
        aqty = chunk["best_ask_qty"].to_numpy(dtype=np.float64)
        valid = (
            np.isfinite(bid)
            & np.isfinite(bqty)
            & np.isfinite(ask)
            & np.isfinite(aqty)
            & (bid > 0)
            & (ask >= bid)
            & (bqty >= 0)
            & (aqty >= 0)
        )
        if not np.all(valid):
            times, bid, bqty, ask, aqty = (
                values[valid] for values in (times, bid, bqty, ask, aqty)
            )
        if len(times) == 0:
            continue
        order = np.argsort(times, kind="stable")
        times, bid, bqty, ask, aqty = (
            values[order] for values in (times, bid, bqty, ask, aqty)
        )
        prev_bid = np.empty_like(bid)
        prev_bqty = np.empty_like(bqty)
        prev_ask = np.empty_like(ask)
        prev_aqty = np.empty_like(aqty)
        if previous is None:
            prev_bid[0], prev_bqty[0], prev_ask[0], prev_aqty[0] = (
                bid[0], bqty[0], ask[0], aqty[0]
            )
        else:
            prev_bid[0], prev_bqty[0], prev_ask[0], prev_aqty[0] = previous
        if len(bid) > 1:
            prev_bid[1:] = bid[:-1]
            prev_bqty[1:] = bqty[:-1]
            prev_ask[1:] = ask[:-1]
            prev_aqty[1:] = aqty[:-1]
        previous = (float(bid[-1]), float(bqty[-1]), float(ask[-1]), float(aqty[-1]))
        ofi = (
            np.where(bid >= prev_bid, bqty, 0.0)
            - np.where(bid <= prev_bid, prev_bqty, 0.0)
            - np.where(ask <= prev_ask, aqty, 0.0)
            + np.where(ask >= prev_ask, prev_aqty, 0.0)
        )
        spread = ask - bid
        mid = (ask + bid) / 2.0
        denom = bqty + aqty
        imbalance = np.divide(
            bqty - aqty,
            denom,
            out=np.zeros_like(denom),
            where=denom > 0,
        )
        frame = pd.DataFrame(
            {
                "bucket_ms": (times // bucket_ms) * bucket_ms,
                "ofi_sum": ofi,
                "spread_sum": spread,
                "book_events": 1,
                "last_bid": bid,
                "last_ask": ask,
                "last_mid": mid,
                "last_book_imbalance": imbalance,
            }
        )
        grouped = frame.groupby("bucket_ms", sort=True, as_index=False).agg(
            ofi_sum=("ofi_sum", "sum"),
            spread_sum=("spread_sum", "sum"),
            book_events=("book_events", "sum"),
            last_bid=("last_bid", "last"),
            last_ask=("last_ask", "last"),
            last_mid=("last_mid", "last"),
            last_book_imbalance=("last_book_imbalance", "last"),
        )
        pieces.append(grouped)
    if not pieces:
        raise ValueError("bookTicker archive produced no valid rows")
    combined = pd.concat(pieces, ignore_index=True)
    combined = combined.groupby("bucket_ms", sort=True, as_index=False).agg(
        ofi_sum=("ofi_sum", "sum"),
        spread_sum=("spread_sum", "sum"),
        book_events=("book_events", "sum"),
        last_bid=("last_bid", "last"),
        last_ask=("last_ask", "last"),
        last_mid=("last_mid", "last"),
        last_book_imbalance=("last_book_imbalance", "last"),
    )
    return combined, rows


def _as_bool(values: pd.Series) -> np.ndarray:
    if values.dtype == bool:
        return values.to_numpy(dtype=bool)
    text = values.astype(str).str.strip().str.lower()
    mapped = text.map({"true": True, "false": False, "1": True, "0": False})
    if mapped.isna().any():
        raise ValueError("is_buyer_maker contains unknown values")
    return mapped.to_numpy(dtype=bool)


def process_agg(blob: bytes, bucket_ms: int) -> tuple[pd.DataFrame, int]:
    pieces: list[pd.DataFrame] = []
    rows = 0
    for chunk in iter_csv_chunks(blob, columns=AGG_COLUMNS):
        rows += len(chunk)
        price = pd.to_numeric(chunk["price"], errors="coerce").to_numpy(dtype=np.float64)
        qty = pd.to_numeric(chunk["quantity"], errors="coerce").to_numpy(dtype=np.float64)
        times = timestamp_ms(chunk["transact_time"])
        buyer_maker = _as_bool(chunk["is_buyer_maker"])
        valid = np.isfinite(price) & np.isfinite(qty) & (price > 0) & (qty > 0)
        if not np.all(valid):
            times, price, qty, buyer_maker = (
                values[valid] for values in (times, price, qty, buyer_maker)
            )
        if len(times) == 0:
            continue
        order = np.argsort(times, kind="stable")
        times, price, qty, buyer_maker = (
            values[order] for values in (times, price, qty, buyer_maker)
        )
        quote = price * qty
        aggressive_buy = np.where(~buyer_maker, quote, 0.0)
        aggressive_sell = np.where(buyer_maker, quote, 0.0)
        frame = pd.DataFrame(
            {
                "bucket_ms": (times // bucket_ms) * bucket_ms,
                "signed_trade_quote": aggressive_buy - aggressive_sell,
                "total_trade_quote": quote,
                "aggressive_buy_quote": aggressive_buy,
                "aggressive_sell_quote": aggressive_sell,
                "trade_count": 1,
                "trade_price_quote": price * quote,
            }
        )
        grouped = frame.groupby("bucket_ms", sort=True, as_index=False).agg(
            signed_trade_quote=("signed_trade_quote", "sum"),
            total_trade_quote=("total_trade_quote", "sum"),
            aggressive_buy_quote=("aggressive_buy_quote", "sum"),
            aggressive_sell_quote=("aggressive_sell_quote", "sum"),
            trade_count=("trade_count", "sum"),
            trade_price_quote=("trade_price_quote", "sum"),
        )
        pieces.append(grouped)
    if not pieces:
        raise ValueError("aggTrades archive produced no valid rows")
    combined = pd.concat(pieces, ignore_index=True)
    combined = combined.groupby("bucket_ms", sort=True, as_index=False).sum()
    combined["trade_vwap"] = np.divide(
        combined["trade_price_quote"],
        combined["total_trade_quote"],
        out=np.full(len(combined), np.nan),
        where=combined["total_trade_quote"].to_numpy() > 0,
    )
    return combined.drop(columns=["trade_price_quote"]), rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bucket-seconds", type=int, default=10)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    symbols = list(config["universe"]["mandatory"])
    dates = list(config["sampling"]["dates"])
    bucket_ms = int(args.bucket_seconds) * 1000
    if bucket_ms <= 0:
        raise ValueError("bucket seconds must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    archives: list[dict[str, object]] = []
    datasets: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for symbol in symbols:
        symbol_dir = args.output / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        for ordinal, day in enumerate(dates, start=1):
            specs = {
                data_type: ArchiveSpec(symbol, data_type, day)
                for data_type in ("bookTicker", "aggTrades")
            }
            blobs: dict[str, bytes] = {}
            failed = False
            for data_type, spec in specs.items():
                try:
                    blob, metadata = fetch_verified(spec)
                except FileNotFoundError:
                    missing.append(spec.url)
                    failed = True
                    break
                blobs[data_type] = blob
                archives.append(metadata)
            if failed:
                continue
            book, book_rows = process_book(blobs["bookTicker"], bucket_ms)
            agg, agg_rows = process_agg(blobs["aggTrades"], bucket_ms)
            merged = book.merge(agg, on="bucket_ms", how="outer", validate="one_to_one")
            merged = merged.sort_values("bucket_ms").reset_index(drop=True)
            for column in (
                "ofi_sum",
                "spread_sum",
                "book_events",
                "signed_trade_quote",
                "total_trade_quote",
                "aggressive_buy_quote",
                "aggressive_sell_quote",
                "trade_count",
            ):
                merged[column] = merged[column].fillna(0.0)
            merged["timestamp"] = pd.to_datetime(merged["bucket_ms"], unit="ms", utc=True)
            merged["symbol"] = symbol
            target = symbol_dir / f"{symbol}-microstructure-10s-{day}.csv.gz"
            columns = ["timestamp", "symbol"] + [
                column
                for column in merged.columns
                if column not in {"timestamp", "symbol", "bucket_ms"}
            ]
            merged[columns].to_csv(
                target,
                index=False,
                compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
                float_format="%.12g",
            )
            key = f"{symbol}_{day}"
            datasets[key] = {
                "path": str(target.relative_to(args.output)),
                "rows": int(len(merged)),
                "book_rows": int(book_rows),
                "agg_trade_rows": int(agg_rows),
                "start": merged["timestamp"].min().isoformat(),
                "end": merged["timestamp"].max().isoformat(),
                "bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            }
            print(
                f"{symbol} {day} {ordinal}/{len(dates)}: "
                f"book={book_rows:,} agg={agg_rows:,} buckets={len(merged):,}",
                flush=True,
            )
    manifest = {
        "schema_version": 1,
        "source": BASE_URL,
        "config_sha256": sha256_file(args.config),
        "bucket_seconds": args.bucket_seconds,
        "symbols": symbols,
        "dates": dates,
        "archives": sorted(
            archives,
            key=lambda row: (str(row["symbol"]), str(row["day"]), str(row["data_type"])),
        ),
        "missing_archives": sorted(missing),
        "datasets": datasets,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "archives": len(archives),
                "missing_archives": len(missing),
                "datasets": len(datasets),
                "rows": sum(int(info["rows"]) for info in datasets.values()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
