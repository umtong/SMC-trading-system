#!/usr/bin/env python3
"""Compatibility entrypoint for Binance metrics archives with UTC string clocks."""

from __future__ import annotations

from datetime import datetime, timezone

import binance_metrics_bundle as base


def epoch_ms(raw: str) -> int:
    """Normalize numeric epoch or Binance's naive UTC timestamp to epoch ms."""

    text = str(raw).strip()
    try:
        value = int(float(text))
    except ValueError:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
        parsed = (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
        value = int(round(parsed.timestamp() * 1000.0))
    else:
        absolute = abs(value)
        if absolute < 10**11:
            value *= 1000
        elif absolute >= 10**15:
            value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible metrics timestamp: {raw}")
    if value % base.INTERVAL_MS != 0:
        raise ValueError(f"metrics timestamp is not on a 5m UTC grid: {raw}")
    return value


base.epoch_ms = epoch_ms

if __name__ == "__main__":
    raise SystemExit(base.main())
