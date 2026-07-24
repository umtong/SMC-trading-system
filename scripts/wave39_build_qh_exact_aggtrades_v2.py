from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

import wave39_build_qh_exact_aggtrades as core

_original_empty_state = core.empty_state
_original_update_grouped_bins = core.update_grouped_bins
_original_build_output = core.build_output


def empty_state(size: int) -> dict[str, np.ndarray]:
    state = _original_empty_state(size)
    shape = (size, 6)
    state.update(
        signed_base=np.zeros(shape, dtype=np.float64),
        buy_base=np.zeros(shape, dtype=np.float64),
        sell_base=np.zeros(shape, dtype=np.float64),
    )
    return state


def update_grouped_bins(
    state: dict[str, np.ndarray],
    indices: np.ndarray,
    bin_indices: np.ndarray,
    timestamps_ms: np.ndarray,
    prices: np.ndarray,
    quantities: np.ndarray,
    quote_values: np.ndarray,
    signed_values: np.ndarray,
    buyer_maker: np.ndarray,
    actual_trade_count: np.ndarray,
) -> None:
    _original_update_grouped_bins(
        state,
        indices,
        bin_indices,
        timestamps_ms,
        prices,
        quantities,
        quote_values,
        signed_values,
        buyer_maker,
        actual_trade_count,
    )
    if not len(indices):
        return
    temporary = pd.DataFrame(
        {
            "idx": indices,
            "bin": bin_indices,
            "signed_base": np.where(buyer_maker, -quantities, quantities),
            "buy_base": np.where(buyer_maker, 0.0, quantities),
            "sell_base": np.where(buyer_maker, quantities, 0.0),
        }
    )
    grouped = temporary.groupby(
        ["idx", "bin"], sort=False, observed=True
    ).sum()
    group_indices = grouped.index.get_level_values(0).to_numpy(dtype=np.int64)
    group_bins = grouped.index.get_level_values(1).to_numpy(dtype=np.int64)
    for key in ("signed_base", "buy_base", "sell_base"):
        state[key][group_indices, group_bins] += grouped[key].to_numpy(
            dtype=np.float64
        )


def build_output(
    state: dict[str, np.ndarray],
    start: pd.Timestamp,
    end: pd.Timestamp,
    symbol: str,
) -> pd.DataFrame:
    output = _original_build_output(state, start, end, symbol)
    for seconds, bins in ((10, 1), (30, 3), (60, 6)):
        total_base = state["base_qty"][:, :bins].sum(axis=1)
        signed_base = state["signed_base"][:, :bins].sum(axis=1)
        buy_base = state["buy_base"][:, :bins].sum(axis=1)
        sell_base = state["sell_base"][:, :bins].sum(axis=1)
        output[f"signed_base_quantity_{seconds}s"] = signed_base
        output[f"aggressive_buy_base_{seconds}s"] = buy_base
        output[f"aggressive_sell_base_{seconds}s"] = sell_base
        output[f"base_imbalance_{seconds}s"] = np.divide(
            signed_base,
            total_base,
            out=np.zeros_like(signed_base),
            where=total_base > 0,
        )
    return output


def amend_manifest(output_dir: Path) -> None:
    manifests = sorted(output_dir.glob("*.manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError(
            f"expected one output manifest in {output_dir}, found {len(manifests)}"
        )
    path = manifests[0]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest["causality"]["buyer_maker_semantics"] = (
        "buyer_is_maker=true is seller-aggressor and receives negative "
        "signed quote and base quantity"
    )
    manifest["causality"]["primary_paper_replication_measure"] = (
        "base_imbalance_10s"
    )
    manifest["causality"]["quote_weighted_imbalance_is_secondary"] = True
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    core.empty_state = empty_state
    core.update_grouped_bins = update_grouped_bins
    core.build_output = build_output
    result = core.main()
    try:
        output_index = sys.argv.index("--output-dir") + 1
    except ValueError as exc:
        raise RuntimeError("--output-dir is required") from exc
    amend_manifest(Path(sys.argv[output_index]))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
