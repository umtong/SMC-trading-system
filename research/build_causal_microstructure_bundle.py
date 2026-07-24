from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
from huggingface_hub import hf_hub_download, list_repo_files

REPO_ID = "ibrahimdaud/binance-btcusdt"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.parquet$")
FORBIDDEN = re.compile(r"(^|_)(fwd|future|forward|label|target|outcome|mfe|mae)(_|$)", re.I)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-05-31")
    parser.add_argument("--out", type=Path, default=Path("research/microstructure_bundle"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.out / ".hf_cache"
    files = []
    for name in list_repo_files(REPO_ID, repo_type="dataset"):
        if not name.startswith("features/BTCUSDT/"):
            continue
        match = DATE_RE.search(name)
        if not match:
            continue
        date = match.group(1)
        if args.start <= date <= args.end:
            files.append((date, name))
    files.sort()
    if not files:
        raise RuntimeError("no feature files found")
    years: dict[int, list[pd.DataFrame]] = {}
    sources: list[dict] = []
    causal_columns: list[str] | None = None
    removed_columns: set[str] = set()
    for date, name in files:
        local = Path(hf_hub_download(repo_id=REPO_ID, filename=name, repo_type="dataset", cache_dir=cache))
        frame = pd.read_parquet(local)
        frame.columns = [str(column) for column in frame.columns]
        forbidden = [column for column in frame.columns if FORBIDDEN.search(column)]
        removed_columns.update(forbidden)
        frame = frame.drop(columns=forbidden, errors="ignore")
        if causal_columns is None:
            causal_columns = list(frame.columns)
        else:
            causal_columns = sorted(set(causal_columns).union(frame.columns))
        frame["source_date"] = date
        years.setdefault(int(date[:4]), []).append(frame)
        sources.append({
            "date": date,
            "repo_path": name,
            "source_sha256": sha256(local),
            "source_bytes": local.stat().st_size,
            "rows": int(len(frame)),
            "removed_columns": forbidden,
        })
        print(json.dumps(sources[-1], sort_keys=True), flush=True)
    outputs = []
    for year, chunks in sorted(years.items()):
        frame = pd.concat(chunks, ignore_index=True, sort=False)
        # Explicit second barrier after union/concat.
        forbidden = [column for column in frame.columns if FORBIDDEN.search(column)]
        if forbidden:
            raise ValueError(f"forbidden columns survived: {forbidden}")
        time_col = next((column for column in ["bar_time_ms", "open_time", "timestamp", "time"] if column in frame.columns), None)
        if time_col is None:
            raise ValueError(f"timestamp column absent: {list(frame.columns)}")
        if time_col == "bar_time_ms":
            frame["time"] = pd.to_datetime(pd.to_numeric(frame[time_col], errors="raise"), unit="ms", utc=True)
        else:
            frame["time"] = pd.to_datetime(frame[time_col], utc=True, errors="raise")
        frame = frame.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
        for column in frame.select_dtypes(include=["float64"]).columns:
            frame[column] = frame[column].astype("float32")
        path = args.out / f"BTCUSDT_causal_microstructure_5m_{year}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        outputs.append({
            "year": year,
            "file": path.name,
            "rows": int(len(frame)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "first_time": frame["time"].min().isoformat(),
            "last_time": frame["time"].max().isoformat(),
            "columns": list(frame.columns),
        })
        if path.stat().st_size >= 95_000_000:
            raise ValueError(f"year file exceeds GitHub limit: {path}")
    manifest = {
        "source_repository": REPO_ID,
        "start": args.start,
        "end": args.end,
        "explicitly_removed_columns": sorted(removed_columns),
        "forbidden_pattern": FORBIDDEN.pattern,
        "outputs": outputs,
        "sources": sources,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    complete = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
    (args.out / "COMPLETE").write_text(complete + "\n", encoding="utf-8")
    # Remove downloader cache before commit.
    import shutil
    shutil.rmtree(cache, ignore_errors=True)
    print(json.dumps({"outputs": outputs, "removed": sorted(removed_columns)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
