#!/usr/bin/env python3
from __future__ import annotations

"""Execution-gap correction for the frozen queue-aware passive L1 study.

The signal, threshold, queue-ahead, entry latency, fill, cost, routing and test
contracts remain unchanged.  The original source aborted the entire experiment
when the scheduled market exit had no BBO observation at most 500 ms old.  A
live market exit would remain pending until the next executable quote.  V2 uses
the first official Binance bookTicker observation at or after the scheduled
exit, provided it arrives within a fixed 2,000 ms emergency-exit delay.  If no
such quote exists the source still fails closed.  The actual later timestamp
occupies the global slot.
"""

import importlib.util
import math
from pathlib import Path
import sys

import numpy as np

SOURCE = Path(__file__).with_name("queue_aware_passive_l1_v1.py")
spec = importlib.util.spec_from_file_location("queue_aware_passive_l1_frozen", SOURCE)
if spec is None or spec.loader is None:
    raise ImportError(f"cannot load {SOURCE}")
module = importlib.util.module_from_spec(spec)
# Dataclass resolves forward references through sys.modules while the frozen
# module is executing.  Register the dynamically loaded module exactly as a
# normal import would; this changes transport only, not any strategy contract.
sys.modules[spec.name] = module
spec.loader.exec_module(module)

MAX_EMERGENCY_EXIT_DELAY_MS = 2_000


def attempt_v2(dd, row, side, score, ttl, horizon, qmult):
    known = int((dd.sec[row] + 1) * 1000)
    arrival = known + module.LATENCY_MS
    p = module.last_quote(dd, arrival)
    if p is None:
        return None
    price = float(dd.bid[p] if side > 0 else dd.ask[p])
    shown = float(dd.bq[p] if side > 0 else dd.aq[p])
    if price <= 0 or shown <= 0:
        return None
    own = min(module.ORDER_NOTIONAL / price, shown * 0.01)
    need = qmult * shown + own
    pending_end = arrival + ttl * 1000
    i = int(np.searchsorted(dd.tt, arrival, side="left"))
    j = int(np.searchsorted(dd.tt, pending_end, side="right"))
    consumed = 0.0
    fill = -1
    for k in range(i, j):
        opposing = bool(dd.tm[k]) if side > 0 else not bool(dd.tm[k])
        through = dd.tp[k] <= price * (1 + 1e-12) if side > 0 else dd.tp[k] >= price * (1 - 1e-12)
        if opposing and through:
            consumed += float(dd.tq[k])
            if consumed >= need:
                fill = int(dd.tt[k])
                break
    if fill < 0:
        return {
            "signal_time_ms": known,
            "free_time_ms": pending_end,
            "filled": False,
            "symbol": dd.symbol,
            "side": side,
            "score": score,
            "gross_log": math.nan,
        }

    scheduled_exit = fill + horizon * 1000 + module.LATENCY_MS
    xp = module.last_quote(dd, scheduled_exit)
    actual_exit = scheduled_exit
    emergency_delay = 0
    if xp is None:
        xp = int(np.searchsorted(dd.bt, scheduled_exit, side="left"))
        if xp >= len(dd.bt):
            raise RuntimeError(f"no exit BBO after scheduled exit {dd.symbol} {dd.day}")
        emergency_delay = int(dd.bt[xp]) - scheduled_exit
        if emergency_delay < 0 or emergency_delay > MAX_EMERGENCY_EXIT_DELAY_MS:
            raise RuntimeError(
                f"exit BBO delay exceeds {MAX_EMERGENCY_EXIT_DELAY_MS} ms: "
                f"{dd.symbol} {dd.day} delay={emergency_delay}"
            )
        actual_exit = int(dd.bt[xp])

    exit_price = float(dd.bid[xp] if side > 0 else dd.ask[xp])
    exit_qty = float(dd.bq[xp] if side > 0 else dd.aq[xp])
    if exit_price <= 0 or own > module.CAPACITY_FRACTION * exit_qty:
        return None
    return {
        "signal_time_ms": known,
        "free_time_ms": actual_exit,
        "filled": True,
        "symbol": dd.symbol,
        "side": side,
        "score": score,
        "gross_log": side * math.log(exit_price / price),
        "fill_time_ms": fill,
        "entry_price": price,
        "exit_price": exit_price,
        "scheduled_exit_time_ms": scheduled_exit,
        "actual_exit_time_ms": actual_exit,
        "emergency_exit_delay_ms": emergency_delay,
    }


module.attempt = attempt_v2

if __name__ == "__main__":
    module.main()
