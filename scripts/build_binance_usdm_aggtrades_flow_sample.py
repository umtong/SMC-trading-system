from __future__ import annotations

import argparse
import gzip
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

BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
COLUMNS = [
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
    day: str

    @property
    def filename(self) -> str:
        return f"{self.symbol}-aggTrades-{self.day}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.symbol}/{self.filename}"


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


def fetch_verified(spec: ArchiveSpec, retries: int = 5) -> tuple[bytes, dict[str, object]]:
    headers = {"User-Agent": "smc-ict-aggtrades-research/1.0"}
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=240)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            checksum = requests.get(spec.url + ".CHECKSUM", headers=headers, timeout=90)
            checksum.raise_for_status()
            expected = parse_checksum(checksum.text)
            actual = sha256_bytes(blob)
            if actual != expected:
                raise ValueError(f"checksum mismatch for {spec.filename}: {actual} != {expected}")
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
            last = exc
            time.sleep(min(2**attempt, 20))
    assert last is not None
    raise last


def member_name(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV member, found {names}")
        return names[0]


def has_header(blob: bytes, member: str) -> bool:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        with archive.open(member) as handle:
            first = handle.readline().decode("utf-8", errors="replace").strip().lower()
    return any(ch.isalpha() for ch in first)


def iter_chunks(blob: bytes, chunksize: int = 750_000) -> Iterator[pd.DataFrame]:
    member = member_name(blob)
    header = 0 if has_header(blob, member) else None
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        with archive.open(member) as handle:
            reader = pd.read_csv(handle, header=header, chunksize=chunksize)
            for chunk in reader:
                if len(chunk.columns) < len(COLUMNS):
                    raise ValueError(f"{member}: expected {len(COLUMNS)} columns, got {len(chunk.columns)}")
                chunk = chunk.iloc[:, : len(COLUMNS)].copy()
                chunk.columns = COLUMNS
                yield chunk


def timestamp_ms(values: pd.Series) -> np.ndarray:
    numeric = pd.to_numeric(values, errors="raise").to_numpy(dtype=np.float64)
    finite = np.abs(numeric[np.isfinite(numeric)])
    if finite.size == 0:
        raise ValueError("timestamp column is empty")
    divisor = 1000.0 if float(np.median(finite)) > 1e14 else 1.0
    return np.rint(numeric / divisor).astype(np.int64)


def as_bool(values: pd.Series) -> np.ndarray:
    if values.dtype == bool:
        return values.to_numpy(dtype=bool)
    mapped = values.astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}
    )
    if mapped.isna().any():
        raise ValueError("is_buyer_maker contains unknown values")
    return mapped.to_numpy(dtype=bool)


def process(blob: bytes, bucket_ms: int) -> tuple[pd.DataFrame, int]:
    pieces: list[pd.DataFrame] = []
    source_rows = 0
    for chunk in iter_chunks(blob):
        source_rows += len(chunk)
        price = pd.to_numeric(chunk["price"], errors="coerce").to_numpy(dtype=np.float64)
        qty = pd.to_numeric(chunk["quantity"], errors="coerce").to_numpy(dtype=np.float64)
        times = timestamp_ms(chunk["transact_time"])
        buyer_maker = as_bool(chunk["is_buyer_maker"])
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
        buy = np.where(~buyer_maker, quote, 0.0)
        sell = np.where(buyer_maker, quote, 0.0)
        frame = pd.DataFrame(
            {
                "bucket_ms": (times // bucket_ms) * bucket_ms,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "signed_trade_quote": buy - sell,
                "total_trade_quote": quote,
                "aggressive_buy_quote": buy,
                "aggressive_sell_quote": sell,
                "trade_count": 1,
                "trade_price_quote": price * quote,
            }
        )
        grouped = frame.groupby("bucket_ms", sort=True, as_index=False).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            signed_trade_quote=("signed_trade_quote", "sum"),
            total_trade_quote=("total_trade_quote", "sum"),
            aggressive_buy_quote=("aggressive_buy_quote", "sum"),
            aggressive_sell_quote=("aggressive_sell_quote", "sum"),
            trade_count=("trade_count", "sum"),
            trade_price_quote=("trade_price_quote", "sum"),
        )
        pieces.append(grouped)
    if not pieces:
        raise ValueError("archive produced no valid aggregate trades")
    combined = pd.concat(pieces, ignore_index=True)
    combined = combined.groupby("bucket_ms", sort=True, as_index=False).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        signed_trade_quote=("signed_trade_quote", "sum"),
        total_trade_quote=("total_trade_quote", "sum"),
        aggressive_buy_quote=("aggressive_buy_quote", "sum"),
        aggressive_sell_quote=("aggressive_sell_quote", "sum"),
        trade_count=("trade_count", "sum"),
        trade_price_quote=("trade_price_quote", "sum"),
    )
    total = combined["total_trade_quote"].to_numpy(dtype=np.float64)
    combined["trade_vwap"] = np.divide(
        combined["trade_price_quote"], total,
        out=np.full(len(combined), np.nan), where=total > 0,
    )
    combined["flow_imbalance"] = np.divide(
        combined["signed_trade_quote"], total,
        out=np.zeros(len(combined)), where=total > 0,
    )
    combined["close_return_bps"] = combined["close"].pct_change().fillna(0.0) * 10_000
    combined["bucket_time"] = pd.to_datetime(combined["bucket_ms"], unit="ms", utc=True)
    return combined.drop(columns=["trade_price_quote"]), source_rows


def deterministic_gzip_csv(frame: pd.DataFrame, path: Path) -> None:
    raw = frame.to_csv(index=False, float_format="%.12g").encode("utf-8")
    with path.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=6, mtime=0) as handle:
            handle.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bucket-seconds", type=int, default=5)
    args = parser.parse_args()
    config_blob = args.config.read_bytes()
    config = json.loads(config_blob)
    symbols = list(config["universe"]["mandatory"])
    dates = list(config["sampling"]["dates"])
    args.output.mkdir(parents=True, exist_ok=True)
    bucket_ms = args.bucket_seconds * 1000

    archives: list[dict[str, object]] = []
    datasets: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for day in dates:
        for symbol in symbols:
            spec = ArchiveSpec(symbol, day)
            key = f"{symbol}_{day}"
            try:
                blob, meta = fetch_verified(spec)
                frame, source_rows = process(blob, bucket_ms)
                path = args.output / f"{symbol}-aggflow-{args.bucket_seconds}s-{day}.csv.gz"
                deterministic_gzip_csv(frame, path)
                archives.append(meta)
                datasets[key] = {
                    "path": path.name,
                    "rows": int(len(frame)),
                    "source_rows": int(source_rows),
                    "start": frame["bucket_time"].iloc[0].isoformat(),
                    "end": frame["bucket_time"].iloc[-1].isoformat(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                print(json.dumps({"ok": key, "rows": len(frame)}), flush=True)
            except FileNotFoundError:
                missing.append(spec.url)
                print(json.dumps({"missing": key}), flush=True)
            except Exception as exc:  # noqa: BLE001
                errors.append({"key": key, "error": repr(exc)})
                print(json.dumps({"error": key, "detail": repr(exc)}), flush=True)

    manifest = {
        "schema_version": 1,
        "source": BASE_URL,
        "config_sha256": sha256_bytes(config_blob),
        "bucket_seconds": args.bucket_seconds,
        "symbols": symbols,
        "dates": dates,
        "archives": archives,
        "missing_archives": missing,
        "errors": errors,
        "datasets": datasets,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if missing or errors or len(datasets) != len(symbols) * len(dates):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
