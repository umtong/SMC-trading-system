from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path


SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    monthly_manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(args.input_dir.rglob("*_manifest.json"))
    ]
    if len(monthly_manifests) != 48:
        raise RuntimeError(f"expected 48 monthly manifests, found {len(monthly_manifests)}")
    outputs = {}
    expected_year_rows = (366 if args.year % 4 == 0 else 365) * 96
    for symbol in SYMBOLS:
        files = sorted(args.input_dir.rglob(f"{symbol}_{args.year}-*_quarterhour_exact.csv.gz"))
        if len(files) != 12:
            raise RuntimeError(f"{symbol}: expected 12 monthly files, found {len(files)}")
        destination = args.output_dir / f"{symbol}_quarterhour_exact_{args.year}.csv.gz"
        header = None
        writer = None
        rows = 0
        nonzero10 = 0
        previous = None
        with gzip.open(destination, "wt", newline="") as output:
            for source_path in files:
                with gzip.open(source_path, "rt", newline="") as source:
                    reader = csv.DictReader(source)
                    fields = list(reader.fieldnames or [])
                    if header is None:
                        header = fields
                        writer = csv.DictWriter(output, fieldnames=header)
                        writer.writeheader()
                    elif fields != header:
                        raise RuntimeError(f"schema drift in {source_path}")
                    assert writer is not None
                    for row in reader:
                        timestamp = int(row["boundary_time_ms"])
                        if previous is not None and timestamp - previous != 900_000:
                            raise RuntimeError(f"{symbol}: boundary gap {previous}->{timestamp}")
                        previous = timestamp
                        rows += 1
                        nonzero10 += int(int(row["post10s_trades"]) > 0)
                        writer.writerow(row)
        if rows != expected_year_rows:
            raise RuntimeError(f"{symbol}: rows {rows} != {expected_year_rows}")
        outputs[symbol] = {
            "path": destination.name,
            "rows": rows,
            "nonzero_post10s_rows": nonzero10,
            "bytes": destination.stat().st_size,
            "sha256": digest(destination),
        }

    manifest = {
        "schema": "wave39-quarterhour-exact-aggtrades-year-v1",
        "year": args.year,
        "symbols": outputs,
        "monthly_manifest_count": len(monthly_manifests),
        "monthly_output_hashes": {
            f"{item['symbol']}-{item['month']}": item["output_sha256"]
            for item in monthly_manifests
        },
        "future_outcomes_included": False,
        "strategy_results_included": False,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
