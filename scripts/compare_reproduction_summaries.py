from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

ARMS = (
    "A_LEADER_V03_BREAK_RETEST_PLUS_V05",
    "B_V07_FIRST_RETURN_LIMIT",
    "C_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
    "D_LEADER_PLUS_V07_FIRST_RETURN_LIMIT",
    "E_LEADER_PLUS_V07_BOUNDARY_ACCEPT_NEXT_OPEN",
)
FIELDS = (
    "trades",
    "wins",
    "net_r",
    "final_equity",
    "cumulative_return",
    "profit_factor",
    "max_drawdown_fraction",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Candidate generated summary. May be repeated.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _difference(actual: Any, expected: Any) -> dict[str, Any]:
    if actual is None or expected is None:
        return {
            "actual": actual,
            "expected": expected,
            "absolute": None if actual == expected else math.inf,
            "relative": None if actual == expected else math.inf,
        }
    left = float(actual)
    right = float(expected)
    absolute = abs(left - right)
    relative = absolute / max(abs(right), 1e-12)
    return {
        "actual": actual,
        "expected": expected,
        "absolute": absolute,
        "relative": relative,
    }


def _compare(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    arm_results: dict[str, Any] = {}
    score_terms: list[float] = []
    exact = True
    for arm in ARMS:
        actual_arm = candidate["arms"][arm]
        expected_arm = reference["arms"][arm]
        fields: dict[str, Any] = {}
        for field in FIELDS:
            diff = _difference(actual_arm.get(field), expected_arm.get(field))
            fields[field] = diff
            if field in {"trades", "wins"}:
                matches = diff["absolute"] == 0
            else:
                matches = (
                    diff["absolute"] is not None
                    and math.isfinite(float(diff["absolute"]))
                    and float(diff["absolute"]) <= 1e-6
                )
            exact = exact and matches
            relative = diff["relative"]
            if relative is not None and math.isfinite(float(relative)):
                score_terms.append(min(float(relative), 1000.0))
            else:
                score_terms.append(1000.0)
        arm_results[arm] = fields
    return {
        "exact_match": exact,
        "mean_capped_relative_error": math.fsum(score_terms) / len(score_terms),
        "arms": arm_results,
    }


def main() -> int:
    args = _args()
    reference = _load(args.reference)
    comparisons: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for item in args.candidate:
        if "=" not in item:
            raise SystemExit(f"candidate must be NAME=PATH: {item}")
        name, raw_path = item.split("=", 1)
        path = Path(raw_path)
        try:
            comparisons[name] = _compare(_load(path), reference)
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
    ranked = sorted(
        (
            (name, result["mean_capped_relative_error"])
            for name, result in comparisons.items()
        ),
        key=lambda item: (item[1], item[0]),
    )
    exact_matches = [
        name for name, result in comparisons.items() if result["exact_match"]
    ]
    payload = {
        "reference": str(args.reference),
        "exact_matches": exact_matches,
        "closest_candidate": None if not ranked else ranked[0][0],
        "comparisons": comparisons,
        "failures": failures,
        "interpretation": (
            "At least one public source exactly reproduces the committed benchmark."
            if exact_matches
            else "No tested public source exactly reproduces the committed benchmark; raw-data provenance remains unresolved."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
