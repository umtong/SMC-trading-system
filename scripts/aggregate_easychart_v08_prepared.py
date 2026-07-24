from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import pickle
import sys
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
            "Load independently prepared V0.8 windows and run the unchanged "
            "chronological shared-equity/shared-slot comparison."
        )
    )
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v08_target_ownership"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    return parser.parse_args()


def _checkpoint_path(prepared_dir: Path, split: str, index: int) -> Path:
    return prepared_dir / f"{split}_{index:02d}.pkl.gz"


def _load_checkpoint(path: Path) -> tuple[dict[str, object], str]:
    if not path.is_file():
        raise FileNotFoundError(f"missing V0.8 prepared checkpoint: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with gzip.open(path, "rb") as handle:
        payload = pickle.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported V0.8 checkpoint schema: {path}")
    return payload, digest


def _build_split(
    split: str,
    windows: Sequence[Sequence[object]],
    prepared_dir: Path,
) -> tuple[comparison.Split, tuple[dict[str, object], ...]]:
    contexts: list[object] = []
    diagnostics: list[dict[str, object]] = []
    authority_sets: dict[str, dict[int, tuple[object, ...]]] = {}
    manifest: list[dict[str, object]] = []

    for index, expected_window in enumerate(windows):
        path = _checkpoint_path(prepared_dir, split, index)
        payload, digest = _load_checkpoint(path)
        if payload.get("split") != split or payload.get("window_index") != index:
            raise ValueError(f"checkpoint identity mismatch: {path}")
        actual_window = tuple(payload.get("window", ()))
        if actual_window != tuple(expected_window):
            raise ValueError(
                f"checkpoint window contract mismatch for {path}: "
                f"{actual_window!r} != {tuple(expected_window)!r}"
            )
        context = payload.get("context")
        if getattr(context, "index", None) != index:
            raise ValueError(f"checkpoint context index mismatch: {path}")
        contexts.append(context)

        rows = payload.get("diagnostics")
        if not isinstance(rows, tuple):
            raise ValueError(f"checkpoint diagnostics are malformed: {path}")
        diagnostics.extend(dict(row) for row in rows)

        stored_sets = payload.get("authority_sets")
        if not isinstance(stored_sets, dict):
            raise ValueError(f"checkpoint authority sets are malformed: {path}")
        if index == 0:
            authority_sets = {str(key): {} for key in stored_sets}
        elif set(authority_sets) != {str(key) for key in stored_sets}:
            raise ValueError(f"authority policy keys differ between checkpoints: {path}")
        for key, values in stored_sets.items():
            authority_sets[str(key)][index] = tuple(values)  # type: ignore[arg-type]

        manifest.append(
            {
                "split": split,
                "window_index": index,
                "window": list(actual_window),
                "path": str(path),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "authority_counts": {
                    str(key): len(value)  # type: ignore[arg-type]
                    for key, value in stored_sets.items()
                },
            }
        )

    built = comparison.Split(
        name=split,
        windows=tuple(tuple(row) for row in windows),
        contexts=tuple(contexts),  # type: ignore[arg-type]
        authority_sets=authority_sets,
        diagnostics=tuple(diagnostics),
    )
    return built, tuple(manifest)


def main() -> int:
    args = _args()
    development, dev_manifest = _build_split(
        "development",
        DEVELOPMENT_WINDOWS,
        args.prepared_dir,
    )
    holdout, holdout_manifest = _build_split(
        "holdout",
        HOLDOUT_WINDOWS,
        args.prepared_dir,
    )
    prepared_splits = {
        "development": development,
        "holdout": holdout,
    }

    original_prepare = comparison._prepare
    original_argv = list(sys.argv)

    def use_checkpoint(
        name: str,
        windows: Sequence[tuple[object, ...]],
        _data_dir: Path,
    ) -> comparison.Split:
        built = prepared_splits[name]
        if tuple(tuple(row) for row in windows) != built.windows:
            raise ValueError(f"aggregate window contract changed for {name}")
        return built

    comparison._prepare = use_checkpoint
    sys.argv = [
        "compare_easychart_v08_target_ownership",
        "--data-dir",
        str(args.prepared_dir),
        "--output-dir",
        str(args.output_dir),
        "--initial-equity",
        str(args.initial_equity),
        "--risk-fraction",
        str(args.risk_fraction),
    ]
    try:
        status = comparison.main()
    finally:
        comparison._prepare = original_prepare
        sys.argv = original_argv

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "aggregated_at_utc": datetime.now(timezone.utc).isoformat(),
        "prepared_dir": str(args.prepared_dir),
        "development": list(dev_manifest),
        "holdout": list(holdout_manifest),
    }
    (args.output_dir / "prepared_checkpoint_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
