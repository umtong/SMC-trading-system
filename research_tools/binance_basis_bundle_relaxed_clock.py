#!/usr/bin/env python3
"""Run the basis bundle builder while preserving valid shortened close clocks.

Binance's spot archive contains a small number of rows whose published close_time
is earlier than the nominal interval end while the 5-minute open-time grid remains
continuous. Such rows are accepted only when close_time is inside the bar; the
source value is preserved and independently audited after download.
"""

from __future__ import annotations

import math
from typing import Sequence

import binance_basis_bundle as base


def normalize_row(
    row: Sequence[str],
    spec: base.DatasetSpec,
    filename: str,
    line_number: int,
) -> list[str]:
    if len(row) < 12:
        raise ValueError(f"short row {filename}:{line_number}")
    output = list(row[:12])
    opened = base.epoch_ms(output[0])
    closed = base.epoch_ms(output[6])
    o, h, l, c = map(float, output[1:5])
    if not all(math.isfinite(value) for value in (o, h, l, c)):
        raise ValueError(f"non-finite OHLC {filename}:{line_number}")
    if spec.price_must_be_positive and min(o, h, l, c) <= 0:
        raise ValueError(f"non-positive price {filename}:{line_number}")
    if h + 1e-15 < max(o, c) or l - 1e-15 > min(o, c) or h < l:
        raise ValueError(f"invalid OHLC geometry {filename}:{line_number}")
    clock = closed - opened
    if clock < 0 or clock > base.INTERVAL_MS - 1:
        raise ValueError(f"close_time outside bar {filename}:{line_number}: {clock}")
    numeric = [float(output[index]) for index in (5, 7, 9, 10)]
    trades = int(float(output[8]))
    if not all(math.isfinite(value) for value in numeric) or trades < 0:
        raise ValueError(f"invalid activity {filename}:{line_number}")
    if spec.validate_activity:
        volume, quote_volume, taker_base, taker_quote = numeric
        if min(volume, quote_volume, taker_base, taker_quote) < 0:
            raise ValueError(f"negative activity {filename}:{line_number}")
        if taker_base > volume + max(1e-9, abs(volume) * 1e-9):
            raise ValueError(f"taker base exceeds volume {filename}:{line_number}")
    output[0] = str(opened)
    output[6] = str(closed)
    output[8] = str(trades)
    return output


base.normalize_row = normalize_row

if __name__ == "__main__":
    raise SystemExit(base.main())
