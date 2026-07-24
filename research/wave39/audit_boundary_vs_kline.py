from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.wave39.wave39_engine import sha256_file


SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


def relative_error(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    scale = np.maximum(np.maximum(np.abs(left), np.abs(right)), 1.0)
    return np.abs(left - right) / scale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audits = {}
    for symbol in SYMBOLS:
        boundary_path = args.data_root / f"{symbol}_quarterhour_exact_{args.year}.csv.gz"
        contract_path = args.data_root / "support" / f"{symbol}_contract_1m_{args.year}.csv.gz"
        boundary = pd.read_csv(boundary_path)
        contract = pd.read_csv(contract_path)
        contract = contract.loc[contract["open_time_ms"].isin(boundary["boundary_time_ms"])].copy()
        contract.sort_values("open_time_ms", inplace=True, kind="mergesort")
        boundary.sort_values("boundary_time_ms", inplace=True, kind="mergesort")
        if len(contract) != len(boundary):
            raise RuntimeError(f"{symbol}: boundary/support alignment count mismatch")
        if not np.array_equal(
            contract["open_time_ms"].to_numpy(np.int64),
            boundary["boundary_time_ms"].to_numpy(np.int64),
        ):
            raise RuntimeError(f"{symbol}: boundary/support timestamps mismatch")
        source_present = contract.get("source_present", pd.Series(1, index=contract.index)).fillna(0).to_numpy(np.int8) == 1
        comparable = source_present & (boundary["post60s_trades"].to_numpy(np.int64) > 0)
        if not comparable.any():
            raise RuntimeError(f"{symbol}: no comparable exact-flow rows")

        fields = {
            "total_quote": (
                boundary["post60s_total_quote"].to_numpy(float),
                contract["quote_volume"].to_numpy(float),
                2.5e-7,
            ),
            "taker_buy_quote": (
                boundary["post60s_buy_quote"].to_numpy(float),
                contract["taker_buy_quote"].to_numpy(float),
                2.5e-7,
            ),
            "open": (
                boundary["post60s_first_price"].to_numpy(float),
                contract["open"].to_numpy(float),
                1e-12,
            ),
            "high": (
                boundary["post60s_high"].to_numpy(float),
                contract["high"].to_numpy(float),
                1e-12,
            ),
            "low": (
                boundary["post60s_low"].to_numpy(float),
                contract["low"].to_numpy(float),
                1e-12,
            ),
            "close": (
                boundary["post60s_last_price"].to_numpy(float),
                contract["close"].to_numpy(float),
                1e-12,
            ),
        }
        field_results = {}
        for name, (left, right, tolerance) in fields.items():
            errors = relative_error(left[comparable], right[comparable])
            violations = errors > tolerance
            violation_count = int(violations.sum())
            if violation_count:
                worst = np.argsort(errors)[-5:][::-1]
                raise RuntimeError(
                    f"{symbol}/{name}: {violation_count} rows exceed {tolerance}; "
                    f"worst={errors[worst].tolist()}"
                )
            field_results[name] = {
                "rows": int(len(errors)),
                "maximum_relative_error": float(errors.max(initial=0.0)),
                "tolerance": tolerance,
                "violations": violation_count,
            }
        missing_contract = int((~source_present).sum())
        flow_without_contract = int(((~source_present) & (boundary["post60s_trades"].to_numpy(np.int64) > 0)).sum())
        audits[symbol] = {
            "boundary_rows": int(len(boundary)),
            "comparable_rows": int(comparable.sum()),
            "missing_contract_boundary_rows": missing_contract,
            "exact_flow_rows_during_contract_gaps": flow_without_contract,
            "fields": field_results,
            "boundary_sha256": sha256_file(boundary_path),
            "contract_sha256": sha256_file(contract_path),
        }
    result = {
        "schema": "wave39-aggtrades-kline-crosscheck-v1",
        "year": args.year,
        "symbols": audits,
        "future_outcomes_used": False,
        "strategy_results_used": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
