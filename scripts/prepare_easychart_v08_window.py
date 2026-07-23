from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import pickle
import time
from typing import Sequence

from scripts import compare_easychart_v08_target_ownership as comparison
from scripts.v08_windows import DEVELOPMENT_WINDOWS, HOLDOUT_WINDOWS


WINDOWS: dict[str, tuple[tuple[object, ...], ...]] = {
    "development": DEVELOPMENT_WINDOWS,
    "holdout": HOLDOUT_WINDOWS,
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build one causal V0.8 feature/authority window and persist an "
            "importable gzip-pickle checkpoint for later global replay."
        )
    )
    parser.add_argument("--split", choices=tuple(WINDOWS), required=True)
    parser.add_argument("--window-index", type=int, required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("prepared/v08"))
    return parser.parse_args()


def _selected_window(split: str, index: int) -> tuple[object, ...]:
    windows = WINDOWS[split]
    if index < 0 or index >= len(windows):
        raise ValueError(
            f"window-index {index} is outside {split} range 0..{len(windows) - 1}"
        )
    return tuple(windows[index])


def _reindex_single_window(
    prepared: comparison.Split,
    *,
    split: str,
    original_index: int,
    window: Sequence[object],
) -> dict[str, object]:
    if len(prepared.contexts) != 1 or len(prepared.windows) != 1:
        raise ValueError("single-window preparation must produce exactly one context")
    context = replace(prepared.contexts[0], index=original_index)
    authority_sets: dict[str, tuple[object, ...]] = {}
    for key, mapping in prepared.authority_sets.items():
        if set(mapping) != {0}:
            raise ValueError(f"unexpected local authority indices for {key}: {set(mapping)}")
        authority_sets[key] = tuple(mapping[0])
    diagnostics = tuple(
        {**row, "window_index": original_index}
        for row in prepared.diagnostics
    )
    return {
        "schema_version": 1,
        "split": split,
        "window_index": original_index,
        "window": tuple(window),
        "context": context,
        "authority_sets": authority_sets,
        "diagnostics": diagnostics,
    }


def _checkpoint_path(output_dir: Path, split: str, index: int) -> Path:
    return output_dir / f"{split}_{index:02d}.pkl.gz"


def _write_checkpoint(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wb", compresslevel=6) as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = _args()
    window = _selected_window(args.split, args.window_index)
    started = time.perf_counter()
    prepared = comparison._prepare(args.split, (window,), args.data_dir)
    payload = _reindex_single_window(
        prepared,
        split=args.split,
        original_index=args.window_index,
        window=window,
    )
    checkpoint = _checkpoint_path(args.output_dir, args.split, args.window_index)
    digest = _write_checkpoint(checkpoint, payload)
    elapsed = time.perf_counter() - started
    authority_sets = payload["authority_sets"]
    assert isinstance(authority_sets, dict)
    metadata = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "split": args.split,
        "window_index": args.window_index,
        "window": list(window),
        "elapsed_seconds": elapsed,
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": digest,
        "checkpoint_bytes": checkpoint.stat().st_size,
        "authority_counts": {
            str(key): len(value)  # type: ignore[arg-type]
            for key, value in authority_sets.items()
        },
    }
    metadata_path = checkpoint.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
