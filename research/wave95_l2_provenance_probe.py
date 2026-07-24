#!/usr/bin/env python3
"""Wave95 L2 provenance probe.

This research-only script verifies whether a pinned third-party reconstruction
of Binance USD-M 20-level order books is sufficiently aligned with official
Binance Vision bookTicker data to justify any downstream L2 alpha research.
It calculates no strategy signal or PnL and opens no new performance holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from huggingface_hub import HfApi, hf_hub_download

REPO_ID = "predict-quant/binance-future-orderbook"
REVISION = "b8590b83452d7a32fbb274ff7741b6db000b3984"
DAY = "2026-04-07"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
SAMPLE_ROWS = 50_000
OFFICIAL_ROOT = "https://data.binance.vision/data/futures/um/daily/bookTicker"
USER_AGENT = "smc-wave95-l2-provenance/1.0"
EXPECTED = {
    "BTCUSDT": {
        "path": "BTCUSDT/2026-04-07_BTCUSDT_depth20.parquet",
        "sha256": "ab5aa7747507238ab24d0cf1a66d4c48e94c0482106a8bdc94492b0326e8bb3b",
        "rows": 797489,
    },
    "ETHUSDT": {
        "path": "ETHUSDT/2026-04-07_ETHUSDT_depth20.parquet",
        "sha256": "d7823c7ecbb012981d0ecc2ae59a70f39feda12dc45263209aef56bdae080b7a",
        "rows": 655000,
    },
    "SOLUSDT": {
        "path": "SOLUSDT/2026-04-07_SOLUSDT_depth20.parquet",
        "sha256": "eb8a1ce3b471e2a1f2a77c86e3543e5a545956bd3f563d7b437fec25e452feb1",
        "rows": 611000,
    },
    "XRPUSDT": {
        "path": "XRPUSDT/2026-04-07_XRPUSDT_depth20.parquet",
        "sha256": "8fb140f85d6a268b03421163cf83442d6f5d892a735b6068df7982e3f0f8e8d0",
        "rows": 773056,
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, attempts: int = 7) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"download failed {url}: {last!r}")


def norm_ms(values: Iterable[object]) -> np.ndarray:
    numeric = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(float)
    finite = np.isfinite(numeric)
    if not bool(finite.any()):
        return np.full(len(numeric), -1, dtype=np.int64)
    median = float(np.nanmedian(np.abs(numeric[finite])))
    if median > 1e17:
        numeric /= 1_000_000
    elif median > 1e14:
        numeric /= 1_000
    elif median < 1e11:
        numeric *= 1_000
    output = np.full(len(numeric), -1, dtype=np.int64)
    output[finite] = numeric[finite].astype(np.int64)
    return output


def official_archive(cache: Path, symbol: str) -> tuple[Path, dict]:
    cache.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-bookTicker-{DAY}.zip"
    url = f"{OFFICIAL_ROOT}/{symbol}/{name}"
    path = cache / name
    checksum_path = cache / f"{name}.CHECKSUM"
    if not path.exists():
        path.write_bytes(fetch(url))
    if not checksum_path.exists():
        checksum_path.write_bytes(fetch(url + ".CHECKSUM"))
    expected = checksum_path.read_text(encoding="utf-8-sig").strip().split()[0].lower()
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"official checksum mismatch {name}: {actual} != {expected}")
    return path, {"url": url, "sha256": actual, "bytes": path.stat().st_size}


def _resolve_column(columns: Iterable[object], aliases: tuple[str, ...]) -> object | None:
    mapping = {str(column).strip().lower().replace(" ", "_"): column for column in columns}
    for alias in aliases:
        if alias in mapping:
            return mapping[alias]
    return None


def _official_chunks(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"unexpected official archive members: {members}")
        with archive.open(members[0]) as probe:
            first = probe.readline().decode("utf-8-sig").strip()
        first_token = first.split(",", 1)[0].strip()
        has_header = any(character.isalpha() or character == "_" for character in first_token)
        with archive.open(members[0]) as raw:
            if has_header:
                yield from pd.read_csv(raw, chunksize=750_000, low_memory=False)
            else:
                names = [
                    "update_id", "best_bid_price", "best_bid_qty", "best_ask_price",
                    "best_ask_qty", "transaction_time", "event_time",
                ]
                yield from pd.read_csv(raw, header=None, names=names, chunksize=750_000, low_memory=False)


def normalize_official_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    time_column = _resolve_column(chunk.columns, ("event_time", "e", "time", "timestamp", "transaction_time", "t"))
    bid_column = _resolve_column(chunk.columns, ("best_bid_price", "bid_price", "bid", "b"))
    bid_qty_column = _resolve_column(chunk.columns, ("best_bid_qty", "bid_qty", "bid_quantity", "bq"))
    ask_column = _resolve_column(chunk.columns, ("best_ask_price", "ask_price", "ask", "a"))
    ask_qty_column = _resolve_column(chunk.columns, ("best_ask_qty", "ask_qty", "ask_quantity", "aq"))
    required = (time_column, bid_column, bid_qty_column, ask_column, ask_qty_column)
    if any(column is None for column in required):
        raise ValueError(f"unrecognized official bookTicker schema: {list(chunk.columns)}")
    output = pd.DataFrame({
        "time_ms": norm_ms(chunk[time_column]),
        "bid": pd.to_numeric(chunk[bid_column], errors="coerce"),
        "bid_qty": pd.to_numeric(chunk[bid_qty_column], errors="coerce"),
        "ask": pd.to_numeric(chunk[ask_column], errors="coerce"),
        "ask_qty": pd.to_numeric(chunk[ask_qty_column], errors="coerce"),
    })
    output = output[
        (output.time_ms >= 0)
        & (output.bid > 0)
        & (output.ask > output.bid)
        & (output.bid_qty >= 0)
        & (output.ask_qty >= 0)
    ]
    return output.sort_values("time_ms", kind="mergesort").drop_duplicates("time_ms", keep="last")


def preceding_official_quotes(path: Path, query_times: np.ndarray) -> pd.DataFrame:
    queries = np.asarray(query_times, dtype=np.int64)
    if len(queries) == 0 or bool(np.any(queries[1:] < queries[:-1])):
        raise ValueError("query times must be nonempty and sorted")
    result = {
        "official_time_ms": np.full(len(queries), -1, dtype=np.int64),
        "official_bid": np.full(len(queries), np.nan),
        "official_bid_qty": np.full(len(queries), np.nan),
        "official_ask": np.full(len(queries), np.nan),
        "official_ask_qty": np.full(len(queries), np.nan),
    }
    pointer = 0
    previous: tuple[int, float, float, float, float] | None = None
    last_seen = -1
    for raw_chunk in _official_chunks(path):
        chunk = normalize_official_chunk(raw_chunk)
        if chunk.empty:
            continue
        if previous is not None:
            extra = pd.DataFrame([previous], columns=["time_ms", "bid", "bid_qty", "ask", "ask_qty"])
            chunk = pd.concat([extra, chunk], ignore_index=True)
            chunk = chunk.sort_values("time_ms", kind="mergesort").drop_duplicates("time_ms", keep="last")
        times = chunk.time_ms.to_numpy(np.int64)
        if last_seen >= 0 and int(times[0]) < last_seen:
            raise ValueError("official archive chronology reversed across chunks")
        last_seen = int(times[-1])
        stop = int(np.searchsorted(queries, times[-1], side="right"))
        if stop > pointer:
            segment = queries[pointer:stop]
            positions = np.searchsorted(times, segment, side="right") - 1
            valid = positions >= 0
            for key, column in (
                ("official_time_ms", "time_ms"),
                ("official_bid", "bid"),
                ("official_bid_qty", "bid_qty"),
                ("official_ask", "ask"),
                ("official_ask_qty", "ask_qty"),
            ):
                values = chunk[column].to_numpy()
                result[key][pointer:stop][valid] = values[positions[valid]]
            pointer = stop
        last = chunk.iloc[-1]
        previous = (int(last.time_ms), float(last.bid), float(last.bid_qty), float(last.ask), float(last.ask_qty))
    if previous is not None and pointer < len(queries):
        for index in range(pointer, len(queries)):
            result["official_time_ms"][index] = previous[0]
            result["official_bid"][index] = previous[1]
            result["official_bid_qty"][index] = previous[2]
            result["official_ask"][index] = previous[3]
            result["official_ask_qty"][index] = previous[4]
    return pd.DataFrame({"query_time_ms": queries, **result})


def parse_levels(value: object) -> list[list[float]]:
    raw = json.loads(value) if isinstance(value, str) else value
    if not isinstance(raw, list):
        raise ValueError("order-book side is not a list")
    output: list[list[float]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            raise ValueError(f"invalid level {level!r}")
        output.append([float(level[0]), float(level[1])])
    return output


def sample_l2(frame: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, dict]:
    count = min(SAMPLE_ROWS, len(frame))
    indices = np.unique(np.linspace(0, len(frame) - 1, count, dtype=np.int64))
    sample = frame.iloc[indices].copy().reset_index(drop=False).rename(columns={"index": "source_row"})
    rows: list[dict] = []
    geometry_valid = 0
    for row in sample.itertuples(index=False):
        bids = parse_levels(row.bids)
        asks = parse_levels(row.asks)
        bid_prices = np.asarray([level[0] for level in bids], dtype=float)
        ask_prices = np.asarray([level[0] for level in asks], dtype=float)
        bid_qty = np.asarray([level[1] for level in bids], dtype=float)
        ask_qty = np.asarray([level[1] for level in asks], dtype=float)
        valid = bool(
            len(bids) == 20
            and len(asks) == 20
            and np.all(np.isfinite(bid_prices))
            and np.all(np.isfinite(ask_prices))
            and np.all(np.isfinite(bid_qty))
            and np.all(np.isfinite(ask_qty))
            and np.all(bid_qty >= 0)
            and np.all(ask_qty >= 0)
            and np.all(np.diff(bid_prices) < 0)
            and np.all(np.diff(ask_prices) > 0)
            and bid_prices[0] < ask_prices[0]
        )
        geometry_valid += int(valid)
        rows.append({
            "symbol": symbol,
            "source_row": int(row.source_row),
            "event_type": str(row.e),
            "event_time_ms": int(row.E),
            "transaction_time_ms": int(row.T),
            "best_bid": float(bid_prices[0]),
            "best_bid_qty": float(bid_qty[0]),
            "best_ask": float(ask_prices[0]),
            "best_ask_qty": float(ask_qty[0]),
            "geometry_valid": valid,
        })
    return pd.DataFrame(rows), {
        "sample_rows": int(len(rows)),
        "geometry_valid_fraction": float(geometry_valid / max(len(rows), 1)),
    }


def match_clock(sample: pd.DataFrame, official: pd.DataFrame, clock_column: str, suffix: str) -> pd.DataFrame:
    lookup = official.set_index("query_time_ms")
    matched = sample.copy()
    query = matched[clock_column].to_numpy(np.int64)
    positions = lookup.index.to_numpy(np.int64).searchsorted(query)
    if not bool(np.array_equal(lookup.index.to_numpy(np.int64)[positions], query)):
        raise RuntimeError("official query lookup mismatch")
    selected = lookup.iloc[positions].reset_index(drop=True)
    age = query - selected.official_time_ms.to_numpy(np.int64)
    price_tolerance = np.maximum(matched.best_bid.abs().to_numpy(float), matched.best_ask.abs().to_numpy(float)) * 1e-12 + 1e-12
    price_match = (
        np.isclose(matched.best_bid, selected.official_bid, rtol=0, atol=price_tolerance)
        & np.isclose(matched.best_ask, selected.official_ask, rtol=0, atol=price_tolerance)
    )
    quantity_match = (
        np.isclose(matched.best_bid_qty, selected.official_bid_qty, rtol=1e-9, atol=1e-12)
        & np.isclose(matched.best_ask_qty, selected.official_ask_qty, rtol=1e-9, atol=1e-12)
    )
    matched[f"official_time_{suffix}_ms"] = selected.official_time_ms.to_numpy(np.int64)
    matched[f"official_age_{suffix}_ms"] = age
    matched[f"price_match_{suffix}"] = price_match
    matched[f"quantity_match_{suffix}"] = quantity_match
    matched[f"full_match_{suffix}"] = price_match & quantity_match
    return matched


def symbol_probe(cache: Path, output: Path, symbol: str) -> tuple[dict, pd.DataFrame]:
    expected = EXPECTED[symbol]
    local = Path(hf_hub_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        filename=expected["path"],
        revision=REVISION,
        cache_dir=str(cache / "huggingface"),
    ))
    actual_sha = sha256_file(local)
    if actual_sha != expected["sha256"]:
        raise ValueError(f"third-party file hash changed {symbol}: {actual_sha}")
    frame = pd.read_parquet(local, columns=["e", "lastUpdateId", "E", "T", "U", "u", "pu", "bids", "asks"])
    if len(frame) != expected["rows"]:
        raise ValueError(f"unexpected row count {symbol}: {len(frame)} != {expected['rows']}")
    for column in ("E", "T", "U", "u", "pu", "lastUpdateId"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.E.isna().any() or frame.T.isna().any():
        raise ValueError(f"missing event clocks {symbol}")
    event_time = frame.E.to_numpy(np.int64)
    transaction_time = frame.T.to_numpy(np.int64)
    day_start = int(pd.Timestamp(DAY, tz="UTC").timestamp() * 1000)
    day_end = day_start + 86_400_000
    depth = frame.e.astype(str).eq("depthUpdate").to_numpy()
    previous_depth = np.r_[False, depth[:-1]]
    sequence_mask = depth & previous_depth & frame.pu.notna().to_numpy() & frame.u.shift(1).notna().to_numpy()
    pu = frame.pu.to_numpy(float)
    previous_u = frame.u.shift(1).to_numpy(float)
    sequence_fraction = float(np.isclose(pu[sequence_mask], previous_u[sequence_mask], rtol=0, atol=0).mean()) if bool(sequence_mask.any()) else math.nan

    sample, sample_metrics = sample_l2(frame, symbol)
    combined_queries = np.unique(np.r_[sample.event_time_ms.to_numpy(np.int64), sample.transaction_time_ms.to_numpy(np.int64)])
    official_path, official_meta = official_archive(cache / "official" / symbol, symbol)
    official = preceding_official_quotes(official_path, combined_queries)
    matched_e = match_clock(sample, official, "event_time_ms", "E")
    matched = match_clock(matched_e, official, "transaction_time_ms", "T")
    valid_e = matched.official_time_E_ms >= 0
    valid_t = matched.official_time_T_ms >= 0
    price_e = float(matched.loc[valid_e, "price_match_E"].mean()) if bool(valid_e.any()) else 0.0
    price_t = float(matched.loc[valid_t, "price_match_T"].mean()) if bool(valid_t.any()) else 0.0
    full_e = float(matched.loc[valid_e, "full_match_E"].mean()) if bool(valid_e.any()) else 0.0
    full_t = float(matched.loc[valid_t, "full_match_T"].mean()) if bool(valid_t.any()) else 0.0
    chosen_clock = "E" if (price_e, full_e) >= (price_t, full_t) else "T"
    valid_chosen = matched[f"official_time_{chosen_clock}_ms"] >= 0
    age = matched.loc[valid_chosen, f"official_age_{chosen_clock}_ms"].to_numpy(float)
    report = {
        "symbol": symbol,
        "third_party": {
            "repo_id": REPO_ID,
            "revision": REVISION,
            "repo_path": expected["path"],
            "sha256": actual_sha,
            "bytes": local.stat().st_size,
            "rows": int(len(frame)),
        },
        "official_book_ticker": official_meta,
        "event_start": pd.to_datetime(event_time.min(), unit="ms", utc=True).isoformat(),
        "event_end": pd.to_datetime(event_time.max(), unit="ms", utc=True).isoformat(),
        "event_time_monotonic_fraction": float(np.mean(np.diff(event_time) >= 0)),
        "event_time_duplicate_fraction": float(pd.Series(event_time).duplicated().mean()),
        "transaction_le_event_fraction": float(np.mean(transaction_time <= event_time)),
        "inside_declared_day_fraction": float(np.mean((event_time >= day_start) & (event_time < day_end))),
        "sequence_links": int(sequence_mask.sum()),
        "pu_matches_previous_u_fraction": sequence_fraction,
        **sample_metrics,
        "official_match_coverage_E": float(valid_e.mean()),
        "official_match_coverage_T": float(valid_t.mean()),
        "price_match_fraction_E": price_e,
        "price_match_fraction_T": price_t,
        "full_price_quantity_match_fraction_E": full_e,
        "full_price_quantity_match_fraction_T": full_t,
        "preferred_clock": chosen_clock,
        "preferred_price_match_fraction": float(matched.loc[valid_chosen, f"price_match_{chosen_clock}"].mean()) if bool(valid_chosen.any()) else 0.0,
        "preferred_full_match_fraction": float(matched.loc[valid_chosen, f"full_match_{chosen_clock}"].mean()) if bool(valid_chosen.any()) else 0.0,
        "preferred_official_age_median_ms": float(np.median(age)) if len(age) else math.nan,
        "preferred_official_age_p99_ms": float(np.quantile(age, 0.99)) if len(age) else math.nan,
    }
    report["provenance_gate"] = bool(
        report["event_time_monotonic_fraction"] >= 0.99999
        and report["transaction_le_event_fraction"] >= 0.999
        and report["inside_declared_day_fraction"] >= 0.999
        and report["geometry_valid_fraction"] >= 0.9999
        and report["pu_matches_previous_u_fraction"] >= 0.98
        and max(report["official_match_coverage_E"], report["official_match_coverage_T"]) >= 0.995
        and report["preferred_price_match_fraction"] >= 0.995
        and report["preferred_full_match_fraction"] >= 0.80
        and report["preferred_official_age_p99_ms"] <= 60_000
    )
    matched.to_parquet(output / f"{symbol}_{DAY}_sample_matches.parquet", index=False, compression="zstd")
    return report, matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    api = HfApi()
    info = api.dataset_info(REPO_ID, revision=REVISION)
    if info.sha != REVISION:
        raise RuntimeError(f"dataset revision mismatch: {info.sha} != {REVISION}")
    reports: list[dict] = []
    for symbol in SYMBOLS:
        report, _ = symbol_probe(args.cache, args.output, symbol)
        reports.append(report)
        print(json.dumps({"symbol": symbol, "gate": report["provenance_gate"], "price_match": report["preferred_price_match_fraction"], "full_match": report["preferred_full_match_fraction"]}), flush=True)
    payload = {
        "study_id": "WAVE95_L2_PROVENANCE_PROBE_V1",
        "purpose": "provenance and timestamp alignment only; no alpha, strategy PnL, risk or deployment decision",
        "day": DAY,
        "third_party_repo": REPO_ID,
        "third_party_revision": REVISION,
        "all_symbols_pass": bool(all(report["provenance_gate"] for report in reports)),
        "symbols": reports,
        "downstream_l2_alpha_allowed": bool(all(report["provenance_gate"] for report in reports)),
        "performance_holdout_opened": False,
        "orders_submitted": False,
        "paper_or_live_started": False,
        "production_enabled": False,
    }
    manifest = args.output / "provenance_report.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            hashes.append(f"{sha256_file(path)}  {path.relative_to(args.output)}")
    (args.output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(json.dumps({"all_symbols_pass": payload["all_symbols_pass"], "orders_submitted": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
