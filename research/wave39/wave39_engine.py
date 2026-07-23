from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from numba import njit


SIDE_LONG = 1
SIDE_SHORT = -1


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
    """Simulate long and short paths from causal next-minute entries.

    Shape is latency, horizon, stop, side(long=0/short=1), event.  A fixed-time
    exit uses the open at entry+horizon.  The exposed minute interval is
    [entry, entry+horizon), and a stop in that interval wins.  Gaps beyond the
    stop fill at the adverse minute open.  There is no target, so there is no
    favorable same-minute ordering assumption.
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
            if not np.isfinite(ep) or ep <= 0.0:
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
                    for minute in range(entry, entry + max_horizon):
                        op = open_price[minute]
                        hi = high_price[minute]
                        lo = low_price[minute]
                        if side == 1:
                            if lo <= stop:
                                stop_i = minute
                                stop_fill = op if op <= stop else stop
                                break
                        else:
                            if hi >= stop:
                                stop_i = minute
                                stop_fill = op if op >= stop else stop
                                break
                    for hi_index in range(h_count):
                        end = entry + int(horizons[hi_index])
                        if stop_i >= 0 and stop_i < end:
                            xp = stop_fill
                            exit_i = stop_i
                            stopped[li, hi_index, si, side_index, event] = 1
                        else:
                            xp = open_price[end]
                            exit_i = end
                        if np.isfinite(xp) and xp > 0.0:
                            gross[li, hi_index, si, side_index, event] = side * np.log(xp / ep)
                            exit_index[li, hi_index, si, side_index, event] = exit_i
    return gross, exit_index, stopped, entry_index, entry_value


@dataclass(frozen=True)
class SymbolPaths:
    symbol: str
    event_time_ms: np.ndarray
    boundary_minute_index: np.ndarray
    entry_time_ms: np.ndarray
    gross: np.ndarray
    exit_index: np.ndarray
    stopped: np.ndarray
    entry_index: np.ndarray
    entry_value: np.ndarray
    minute_time_ms: np.ndarray
    funding_time_ms: np.ndarray
    funding_rate: np.ndarray
    horizons: np.ndarray
    stops: np.ndarray
    latencies: np.ndarray

    def outcome(
        self,
        *,
        side: np.ndarray,
        horizon_index: int,
        stop_index: int,
        latency_index: int,
        round_trip_bp: float,
        stop_extra_bp: float = 4.0,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        side = np.asarray(side, dtype=np.int8)
        long_mask = side > 0
        event_count = len(side)
        gross = np.full(event_count, np.nan)
        exits = np.full(event_count, -1, np.int64)
        stopped = np.zeros(event_count, np.uint8)
        for side_index, mask in ((0, long_mask), (1, ~long_mask)):
            gross[mask] = self.gross[
                latency_index, horizon_index, stop_index, side_index, mask
            ]
            exits[mask] = self.exit_index[
                latency_index, horizon_index, stop_index, side_index, mask
            ]
            stopped[mask] = self.stopped[
                latency_index, horizon_index, stop_index, side_index, mask
            ]
        entry_indices = self.entry_index[latency_index]
        valid = np.isfinite(gross) & (exits >= 0) & (entry_indices >= 0)
        exit_time_ms = np.full(event_count, -1, np.int64)
        exit_time_ms[valid] = self.minute_time_ms[exits[valid]]
        entry_time_ms = np.full(event_count, -1, np.int64)
        entry_time_ms[valid] = self.minute_time_ms[entry_indices[valid]]

        funding_sum = np.zeros(event_count, dtype=np.float64)
        if len(self.funding_time_ms):
            prefix = np.concatenate(([0.0], np.cumsum(self.funding_rate, dtype=np.float64)))
            left = np.searchsorted(self.funding_time_ms, entry_time_ms, side="right")
            right = np.searchsorted(self.funding_time_ms, exit_time_ms, side="right")
            funding_sum[valid] = prefix[right[valid]] - prefix[left[valid]]
        net = gross - side.astype(np.float64) * funding_sum
        net -= float(round_trip_bp) / 10000.0
        net -= stopped.astype(np.float64) * float(stop_extra_bp) / 10000.0
        net[~valid] = np.nan
        return net, entry_time_ms, exit_time_ms, stopped


def greedy_one_slot(
    eligible_indices: np.ndarray,
    entry_time_ms: np.ndarray,
    exit_time_ms: np.ndarray,
) -> np.ndarray:
    selected: list[int] = []
    occupied_until = -1
    for raw_index in eligible_indices:
        index = int(raw_index)
        entry = int(entry_time_ms[index])
        exit_time = int(exit_time_ms[index])
        if entry < 0 or exit_time < entry:
            continue
        if entry <= occupied_until:
            continue
        selected.append(index)
        occupied_until = exit_time
    return np.asarray(selected, dtype=np.int64)


def max_drawdown_from_logs(log_returns: np.ndarray) -> float:
    if len(log_returns) == 0:
        return 0.0
    equity = np.exp(np.cumsum(log_returns, dtype=np.float64))
    peaks = np.maximum.accumulate(np.concatenate(([1.0], equity)))
    path = np.concatenate(([1.0], equity))
    drawdowns = 1.0 - path / peaks
    return float(np.max(drawdowns))


def metrics(
    log_returns: np.ndarray,
    entry_time_ms: np.ndarray,
    *,
    fold_edges_ms: Iterable[tuple[int, int]],
) -> dict[str, object]:
    values = np.asarray(log_returns, dtype=np.float64)
    times = np.asarray(entry_time_ms, dtype=np.int64)
    finite = np.isfinite(values)
    values = values[finite]
    times = times[finite]
    positive = values[values > 0.0]
    negative = values[values < 0.0]
    gross_profit = float(positive.sum())
    gross_loss = float(-negative.sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0.0 else float("inf")
    ordered_winners = np.sort(positive)[::-1]
    top10 = float(ordered_winners[:10].sum())
    top5 = float(ordered_winners[:5].sum())
    winner_sum = float(ordered_winners.sum())

    if len(times):
        dt = pd.to_datetime(times, unit="ms", utc=True)
        month_labels = dt.strftime("%Y-%m")
        month_values = pd.Series(values).groupby(month_labels).sum()
        positive_months = int((month_values > 0.0).sum())
        month_count = int(len(month_values))
        worst_month = float(month_values.min())
    else:
        month_values = pd.Series(dtype=float)
        positive_months = 0
        month_count = 0
        worst_month = float("nan")

    fold_values: list[float] = []
    for start, end in fold_edges_ms:
        mask = (times >= start) & (times < end)
        fold_values.append(float(values[mask].sum()))

    return {
        "trades": int(len(values)),
        "net_log_growth": float(values.sum()),
        "final_multiple": float(np.exp(values.sum())),
        "profit_factor": float(profit_factor),
        "max_drawdown": max_drawdown_from_logs(values),
        "wins": int((values > 0.0).sum()),
        "losses": int((values < 0.0).sum()),
        "positive_months": positive_months,
        "month_count": month_count,
        "worst_month_log_growth": worst_month,
        "fold_log_growth": fold_values,
        "positive_folds": int(sum(value > 0.0 for value in fold_values)),
        "minimum_fold_log_growth": float(min(fold_values)) if fold_values else float("nan"),
        "top5_winner_share": top5 / winner_sum if winner_sum > 0.0 else float("nan"),
        "top10_winner_share": top10 / winner_sum if winner_sum > 0.0 else float("nan"),
        "net_after_top5": float(values.sum() - top5),
        "net_after_top10": float(values.sum() - top10),
        "monthly_log_growth": {str(key): float(value) for key, value in month_values.items()},
    }


def stable_candidate_id(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
