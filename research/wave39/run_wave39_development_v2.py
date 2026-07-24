from __future__ import annotations

import numpy as np

from research.wave39 import run_wave39_development as base


_original = base.candidate_mask_and_side


def candidate_mask_and_side_v2(
    *,
    family: str,
    window: int,
    components,
    thresholds,
    q_imbalance: float,
    q_volume: float,
):
    if family != "BOUNDARY_FLOW_FLIP":
        return _original(
            family=family,
            window=window,
            components=components,
            thresholds=thresholds,
            q_imbalance=q_imbalance,
            q_volume=q_volume,
        )
    if window != 60:
        raise ValueError("flow flip is defined at the completed 60-second clock")
    first_imbalance = components["imbalance"][10]
    first_total = components["total"][10]
    first_net = components["net"][10]
    incremental_net = components["net"][60] - first_net
    first_side = np.sign(first_net).astype(np.int8)
    side = np.sign(incremental_net).astype(np.int8)
    first_last = components["last"][10]
    final_last = components["last"][60]
    incremental_return = np.log(final_last / first_last)
    extreme_first_clock = (
        np.abs(first_imbalance) >= thresholds[("imbalance", 10, q_imbalance)]
    ) & (first_total >= thresholds[("volume", 10, q_volume)])
    mask = (
        extreme_first_clock
        & (side != 0)
        & (first_side != 0)
        & (side == -first_side)
        & (side.astype(np.float64) * incremental_return > 0.0)
    )
    return mask, side


base.candidate_mask_and_side = candidate_mask_and_side_v2


if __name__ == "__main__":
    raise SystemExit(base.main())
