from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import argparse
import hashlib
import json
import re
import zipfile

import pandas as pd
import requests

SYMBOLS = ("BTCUSDT", "ETHUSDT")
MONTHS = tuple([f"2025-{month:02d}" for month in range(1, 13)] + ["2026-01"])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_bytes(session: requests.Session, url: str) -> bytes:
    for attempt in range(6):
        response = session.get(url, timeout=240)
        if response.status_code == 200:
            return response.content
        if response.status_code == 404:
            raise FileNotFoundError(url)
        if attempt == 5:
            response.raise_for_status()
    raise RuntimeError(url)


def archive_url(market: str, kind: str, symbol: str, month: str) -> str:
    name = f"{symbol}-1m-{month}.zip"
    if market == "spot":
        return f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{name}"
    return f"https://data.binance.vision/data/futures/um/monthly/{kind}/{symbol}/1m/{name}"


def archive_folder(root: Path, market: str, kind: str, symbol: str) -> Path:
    return root / market / (symbol if market == "spot" else f"{kind}/{symbol}")


def ensure_archive(
    session: requests.Session,
    root: Path,
    market: str,
    kind: str,
    symbol: str,
    month: str,
) -> dict[str, object]:
    url = archive_url(market, kind, symbol, month)
    name = f"{symbol}-1m-{month}.zip"
    folder = archive_folder(root, market, kind, symbol)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    checksum_path = folder / f"{name}.CHECKSUM"
    if not checksum_path.exists():
        checksum_path.write_bytes(get_bytes(session, url + ".CHECKSUM"))
    match = re.search(rb"([0-9a-fA-F]{64})", checksum_path.read_bytes())
    if not match:
        raise ValueError(f"bad checksum document: {checksum_path}")
    expected = match.group(1).decode().lower()
    if not path.exists() or sha256(path) != expected:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(get_bytes(session, url))
        actual = sha256(temporary)
        if actual != expected:
            raise ValueError(f"checksum mismatch: {url}: {actual} != {expected}")
        temporary.replace(path)
    return {
        "market": market,
        "kind": kind,
        "symbol": symbol,
        "month": month,
        "url": url,
        "path": str(path),
        "sha256": expected,
        "bytes": path.stat().st_size,
    }


def read_kline(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise ValueError(f"unexpected ZIP members: {path}")
        raw = pd.read_csv(archive.open(members[0]), header=None)
    if raw.shape[1] < 11:
        raise ValueError(f"bad kline schema: {path}")
    frame = raw.iloc[:, [0, 1, 2, 3, 4, 7, 8, 10]].copy()
    frame.columns = [
        "raw_time",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
        "trades",
        "taker_buy_quote",
    ]
    timestamps = pd.to_numeric(frame.raw_time, errors="raise").astype("int64")
    unit = "us" if float(timestamps.median()) > 1e14 else "ms"
    frame["open_time"] = pd.to_datetime(timestamps, unit=unit, utc=True)
    for column in (
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
        "trades",
        "taker_buy_quote",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    return frame[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "quote_volume",
            "trades",
            "taker_buy_quote",
        ]
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    jobs = [
        (market, kind, symbol, month)
        for symbol in SYMBOLS
        for month in MONTHS
        for market, kind in (
            ("spot", "klines"),
            ("futures", "klines"),
            ("futures", "markPriceKlines"),
        )
    ]
    session = requests.Session()
    manifest: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(ensure_archive, session, args.cache, *job): job for job in jobs
        }
        for future in as_completed(futures):
            manifest.append(future.result())
    pd.DataFrame(manifest).sort_values(
        ["market", "symbol", "month", "kind"]
    ).to_csv(args.output / "input_manifest.csv", index=False)

    summaries: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        spot_parts: list[pd.DataFrame] = []
        futures_parts: list[pd.DataFrame] = []
        for month in MONTHS:
            spot_parts.append(
                read_kline(args.cache / "spot" / symbol / f"{symbol}-1m-{month}.zip")
            )
            futures_frame = read_kline(
                args.cache
                / "futures"
                / "klines"
                / symbol
                / f"{symbol}-1m-{month}.zip"
            )
            mark_frame = read_kline(
                args.cache
                / "futures"
                / "markPriceKlines"
                / symbol
                / f"{symbol}-1m-{month}.zip"
            )[["open_time", "close"]].rename(columns={"close": "mark_close"})
            merged = futures_frame.merge(
                mark_frame, on="open_time", how="left", validate="one_to_one"
            )
            merged = merged.loc[merged.mark_close.notna()].copy()
            if merged.empty:
                raise ValueError(f"no contract/mark overlap: {symbol} {month}")
            futures_parts.append(merged)

        for kind, frame in (
            ("spot", pd.concat(spot_parts, ignore_index=True)),
            ("fut", pd.concat(futures_parts, ignore_index=True)),
        ):
            frame = frame.sort_values("open_time", kind="mergesort").reset_index(
                drop=True
            )
            if frame.open_time.duplicated().any():
                raise ValueError(f"{symbol} {kind}: duplicate minute")
            differences = frame.open_time.diff()
            frame["segment_id"] = (
                differences.isna() | differences.ne(pd.Timedelta(minutes=1))
            ).cumsum().astype("int64") - 1
            output_path = args.output / f"{symbol}_{kind}_1m.csv.gz"
            frame.to_csv(
                output_path,
                index=False,
                compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
            )
            summaries.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "rows": len(frame),
                    "segments": int(frame.segment_id.nunique()),
                    "first": str(frame.open_time.min()),
                    "last": str(frame.open_time.max()),
                    "sha256": sha256(output_path),
                    "bytes": output_path.stat().st_size,
                }
            )
    (args.output / "summary.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
