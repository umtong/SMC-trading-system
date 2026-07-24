from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from ictbt.easychart_v0.research_protocol import (
    DEFAULT_WARMUP_DAYS,
    DEFAULT_WINDOW_DAYS,
    DEFAULT_YEARS,
    generate_annual_samples,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate reproducible 28-day annual BTC/ETH research windows "
            "without reusing the same year/start pair."
        )
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--warmup-days", type=int, default=DEFAULT_WARMUP_DAYS)
    parser.add_argument(
        "--available-through",
        type=date.fromisoformat,
        required=True,
        help="last fully complete UTC data date, YYYY-MM-DD",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    samples = generate_annual_samples(
        years=args.years,
        sample_count=args.samples,
        seed=args.seed,
        available_through=args.available_through,
        window_days=args.window_days,
        warmup_days=args.warmup_days,
    )
    payload = {
        "schema": "ictbt.random_annual_windows.v1",
        "parameters": {
            "seed": args.seed,
            "sample_count": args.samples,
            "years": sorted(set(args.years)),
            "window_days": args.window_days,
            "warmup_days": args.warmup_days,
            "available_through": args.available_through.isoformat(),
            "portfolio_operating_days_per_sample": sum(
                window.operating_days for window in samples[0].windows
            ),
        },
        "samples": [sample.to_dict() for sample in samples],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["parameters"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
