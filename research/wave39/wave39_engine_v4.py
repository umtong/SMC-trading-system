from __future__ import annotations

import numpy as np
from numba import njit

from research.wave39.wave39_engine_v3 import (  # re-export audited gap-aware features/utilities
    SymbolPaths,
    greedy_one_slot,
    max_drawdown_from_logs,
    metrics,
    prior_atr_and_trends,
    sha256_file,
    stable_candidate_id,
)


@njit(cache=True)
def simulate_stop_time_paths(
    open_price: np.ndarray,
    high_price: np.ndarray,
    low_price: np.ndarray,
    boundary_minute_index: np.ndarray,
    atr_at_boundary: np.ndarray,
    horizons: np.ndarray,
    stop_multipliers: np.ndarray,
    latency_minutes: np.ndarray,
):
    """Gap-safe conservative replay.

    A scheduled time exit is invalid if any official minute is absent before it.
    A stop exit remains valid when the stop was observed before a later source
    gap, because no unavailable data were needed to determine that completed
    trade.  No price is imputed and no gap can be crossed by an open trade.
    """
    event_count = boundary_minute_index.shape[0]
    l_count = latency_minutes.shape[0]
    h_count = horizons.shape[0]
    s_count = stop_multipliers.shape[0]
    gross = np.full((l_count, h_count, s_count, 2, event_count), np.nan)
    exit_index = np.full((l_count, h_count, s_count, 2, event_count), -1, np.int64)
    stopped = np.zeros((l_count, h_count, s_count, 2, event_count), np.uint8)
    entry_index = np.full((l_count, event_count), -1, np.int64)
    entry_value = np.full((l_count, event_count), np.nan)

    max_horizon = int(np.max(horizons))
    n = open_price.shape[0]
    for event in range(event_count):
        base_index = int(boundary_minute_index[event])
        atr = atr_at_boundary[event]
        if not np.isfinite(atr) or atr <= 0.0:
            continue
        for li in range(l_count):
            entry = base_index + int(latency_minutes[li])
            if entry < 0 or entry + max_horizon >= n:
                continue
            ep = open_price[entry]
            if (
                not np.isfinite(ep)
                or ep <= 0.0
                or not np.isfinite(high_price[entry])
                or not np.isfinite(low_price[entry])
            ):
                continue
            entry_index[li, event] = entry
            entry_value[li, event] = ep
            for side_index in range(2):
                side = 1 if side_index == 0 else -1
                for si in range(s_count):
                    distance = atr * stop_multipliers[si]
                    stop = ep - distance if side == 1 else ep + distance
                    stop_i = -1
                    stop_fill = np.nan
                    missing_i = -1
                    for minute in range(entry, entry + max_horizon + 1):
                        op = open_price[minute]
                        hi = high_price[minute]
                        lo = low_price[minute]
                        if (
                            not np.isfinite(op)
                            or op <= 0.0
                            or not np.isfinite(hi)
                            or not np.isfinite(lo)
                        ):
                            missing_i = minute
                            break
                        if minute < entry + max_horizon and stop_i < 0:
                            if side == 1 and lo <= stop:
                                stop_i = minute
                                stop_fill = op if op <= stop else stop
                            elif side == -1 and hi >= stop:
                                stop_i = minute
                                stop_fill = op if op >= stop else stop
                    for hi_index in range(h_count):
                        end = entry + int(horizons[hi_index])
                        if stop_i >= 0 and stop_i < end:
                            if missing_i >= 0 and missing_i <= stop_i:
                                continue
                            xp = stop_fill
                            resolved_exit = stop_i
                            stopped[li, hi_index, si, side_index, event] = 1
                        else:
                            if missing_i >= 0 and missing_i <= end:
                                continue
                            xp = open_price[end]
                            resolved_exit = end
                        if np.isfinite(xp) and xp > 0.0:
                            gross[li, hi_index, si, side_index, event] = side * np.log(xp / ep)
                            exit_index[li, hi_index, si, side_index, event] = resolved_exit
    return gross, exit_index, stopped, entry_index, entry_value
