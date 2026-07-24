from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

EXPECTED_BASE_SHA256 = "66b08521a6136ce26fe1c586dcd915877e874228692b03e312fa834afe9554af"
EXPECTED_EXPERIMENT_SHA256 = "8fd640512981ac81f9842a4e63390400b2629b8360dd9e921636ff338c44ad03"
EXPECTED_CHECKPOINT_SHA256 = "884015977d01118526227447467960c098a7dae93dd16cfda65b388e7ecccc93"
EXPECTED_REVISION = "0113be29cdcb7e977037d192c1055c01cf0d369e"
EXPECTED_MANIFEST_DIGEST = "7e29ab8b29e9632c2cc26c525f0ff7f3e57d8791828b22caf3501836de655e2d"
TRIM_START = pd.Timestamp("2021-10-01", tz="UTC")
TRIM_END = pd.Timestamp("2024-01-01", tz="UTC")
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def choose_time_col(schema: pa.Schema, kind: str) -> str:
    candidates = {
        "kline": ("open_time", "open_time_ms", "timestamp", "time", "date"),
        "metrics": ("create_time", "create_time_ms", "timestamp", "time", "date"),
        "funding": ("calc_time", "funding_time", "funding_time_ms", "timestamp", "time", "date"),
    }[kind]
    for c in candidates:
        if c in schema.names:
            return c
    raise KeyError(f"no time column for {kind}: {schema.names}")


def first_finite_value(path: Path, col: str) -> Any:
    pf = pq.ParquetFile(path)
    for i in range(pf.num_row_groups):
        arr = pf.read_row_group(i, columns=[col]).column(0)
        if len(arr) == 0:
            continue
        for value in arr.to_pylist():
            if value is not None:
                return value
    raise ValueError(f"no finite time value in {path}:{col}")


def bound_scalar(ts: pd.Timestamp, field: pa.Field, sample: Any) -> pa.Scalar:
    typ = field.type
    if pa.types.is_timestamp(typ):
        return pa.scalar(ts.to_pydatetime(), type=typ)
    if pa.types.is_date(typ):
        return pa.scalar(ts.date(), type=typ)
    if pa.types.is_integer(typ) or pa.types.is_floating(typ):
        value = float(sample)
        magnitude = abs(value)
        unit = "ns" if magnitude >= 1e17 else "us" if magnitude >= 1e14 else "ms" if magnitude >= 1e11 else "s"
        divisor = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}[unit]
        return pa.scalar(int(ts.timestamp() * divisor), type=typ)
    if pa.types.is_string(typ) or pa.types.is_large_string(typ):
        return pa.scalar(ts.isoformat(), type=typ)
    raise TypeError(f"unsupported time type {typ}")


def trim_parquet(source: Path, destination: Path, kind: str) -> dict[str, Any]:
    dataset = ds.dataset(str(source), format="parquet")
    tcol = choose_time_col(dataset.schema, kind)
    sample = first_finite_value(source, tcol)
    field = dataset.schema.field(tcol)
    lo = bound_scalar(TRIM_START, field, sample)
    hi = bound_scalar(TRIM_END, field, sample)
    table = dataset.to_table(filter=(ds.field(tcol) >= lo) & (ds.field(tcol) < hi))
    if table.num_rows == 0:
        raise RuntimeError(f"empty causal trim {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd", use_dictionary=True)

    raw_time = table[tcol].to_pandas()
    if pd.api.types.is_datetime64_any_dtype(raw_time):
        parsed = pd.to_datetime(raw_time, utc=True)
    else:
        numeric = pd.to_numeric(raw_time, errors="coerce")
        median = float(numeric.dropna().abs().median())
        unit = "ns" if median >= 1e17 else "us" if median >= 1e14 else "ms" if median >= 1e11 else "s"
        parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    if parsed.isna().any():
        raise RuntimeError(f"unparseable time values {source}")
    if parsed.min() < TRIM_START or parsed.max() >= TRIM_END:
        raise RuntimeError(f"trim boundary violation {source}: {parsed.min()} .. {parsed.max()}")
    return {
        "kind": kind,
        "time_column": tcol,
        "rows": int(table.num_rows),
        "min_time": parsed.min().isoformat(),
        "max_time": parsed.max().isoformat(),
        "source_bytes": source.stat().st_size,
        "source_sha256": sha256_file(source),
        "trimmed_bytes": destination.stat().st_size,
        "trimmed_sha256": sha256_file(destination),
    }


def clean_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo_id": manifest["repo_id"],
        "revision": manifest["revision"],
        "manifest_digest": manifest["manifest_digest"],
        "files": {
            symbol: {
                kind: {k: v for k, v in record.items() if k != "local_path"}
                for kind, record in manifest["files"][symbol].items()
            }
            for symbol in SYMBOLS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-script", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    observed = {
        "base_script_sha256": sha256_file(args.base_script),
        "experiment_sha256": sha256_file(args.experiment),
        "checkpoint_sha256": sha256_file(args.checkpoint),
    }
    expected = {
        "base_script_sha256": EXPECTED_BASE_SHA256,
        "experiment_sha256": EXPECTED_EXPERIMENT_SHA256,
        "checkpoint_sha256": EXPECTED_CHECKPOINT_SHA256,
    }
    if observed != expected:
        raise RuntimeError({"source_hash_mismatch": observed, "expected": expected})

    base = import_module("wave84_exact_base", args.base_script)
    experiment = import_module("wave84_exact_experiment", args.experiment)
    checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
    manifest = base.download_sources(args.cache_dir)
    if manifest["revision"] != EXPECTED_REVISION or manifest["manifest_digest"] != EXPECTED_MANIFEST_DIGEST:
        raise RuntimeError({"source_identity_mismatch": {"revision": manifest["revision"], "manifest_digest": manifest["manifest_digest"]}})
    if checkpoint["source_revision"] != manifest["revision"] or checkpoint["source_manifest_digest"] != manifest["manifest_digest"]:
        raise RuntimeError("checkpoint/source identity mismatch")

    trim_root = args.output_dir / "trimmed_source"
    trimmed_manifest = json.loads(json.dumps(manifest))
    trim_audit: dict[str, Any] = {}
    for symbol in SYMBOLS:
        trim_audit[symbol] = {}
        for kind in ("kline", "metrics", "funding"):
            source = Path(manifest["files"][symbol][kind]["local_path"])
            destination = trim_root / symbol / f"{kind}.parquet"
            trim_audit[symbol][kind] = trim_parquet(source, destination, kind)
            trimmed_manifest["files"][symbol][kind]["local_path"] = str(destination)

    index, frames, fundings = base.load_stage("development", trimmed_manifest)
    if index.min() != pd.Timestamp("2022-01-01", tz="UTC") or index.max() >= pd.Timestamp("2024-01-01", tz="UTC"):
        raise RuntimeError(f"unexpected development index {index.min()} .. {index.max()}")
    panel = experiment.build_event_panel(base, checkpoint, index, frames, fundings, with_outcomes=True)
    panel = panel.sort_values(["signal_time", "rank_mean", "symbol"], ascending=[True, False, True], kind="mergesort").reset_index(drop=True)
    panel["source_event_index"] = np.arange(len(panel), dtype=np.int64)

    outputs: dict[str, Any] = {}
    for symbol in SYMBOLS:
        frame = frames[symbol].copy().reset_index()
        if pd.to_datetime(frame["time"], utc=True).max() >= pd.Timestamp("2024-01-01", tz="UTC"):
            raise RuntimeError(f"future row in {symbol} frame")
        feature_path = args.output_dir / f"{symbol}_features_2022_2023.parquet"
        frame.to_parquet(feature_path, index=False, compression="zstd")
        funding = fundings[symbol].copy()
        funding["time"] = pd.to_datetime(funding["time"], utc=True)
        funding = funding[(funding.time >= pd.Timestamp("2022-01-01", tz="UTC")) & (funding.time < pd.Timestamp("2024-01-01", tz="UTC"))]
        funding_path = args.output_dir / f"{symbol}_funding_2022_2023.parquet"
        funding.to_parquet(funding_path, index=False, compression="zstd")
        outputs[feature_path.name] = {"rows": int(len(frame)), "sha256": sha256_file(feature_path), "bytes": feature_path.stat().st_size}
        outputs[funding_path.name] = {"rows": int(len(funding)), "sha256": sha256_file(funding_path), "bytes": funding_path.stat().st_size}

    signal_time = pd.to_datetime(panel.signal_time, utc=True)
    for year in (2022, 2023):
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
        year_panel = panel[(signal_time >= start) & (signal_time < end)].copy()
        panel_path = args.output_dir / f"event_panel_{year}_exact.csv.gz"
        year_panel.to_csv(panel_path, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
        event_keys = [
            (str(row.signal_time), str(row.symbol), int(row.side), str(row.entry_time), str(row.exit_time), str(row.exit_reason))
            for row in year_panel.itertuples(index=False)
        ]
        outputs[panel_path.name] = {
            "rows": int(len(year_panel)),
            "stops": int(year_panel.exit_reason.isin(["protective_stop", "gap_stop"]).sum()),
            "survivors": int(year_panel.stop_survived.astype(bool).sum()),
            "event_digest": hashlib.sha256(json.dumps(event_keys, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "sha256": sha256_file(panel_path),
            "bytes": panel_path.stat().st_size,
        }

    audit = {
        "study_id": "WAVE84_EXACT_PRE2024_STOP_STATE_INPUT_V1",
        "contract": "exact frozen source; exact pinned revision; pre-2024 rows only",
        "source_hashes": observed,
        "source_manifest": clean_manifest(manifest),
        "trim_start": TRIM_START.isoformat(),
        "trim_end_exclusive": TRIM_END.isoformat(),
        "trim_audit": trim_audit,
        "development_index": {"rows": int(len(index)), "start": index.min().isoformat(), "end": index.max().isoformat()},
        "outputs": outputs,
        "later_than_2023_rows_exported": False,
        "2025_path_outcomes_evaluated": False,
        "2026_opened": False,
        "orders_submitted": False,
        "paper_or_live_started": False,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"events_2022": outputs["event_panel_2022_exact.csv.gz"], "events_2023": outputs["event_panel_2023_exact.csv.gz"], "manifest": str(manifest_path)}, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
