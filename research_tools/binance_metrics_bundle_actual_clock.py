#!/usr/bin/env python3
"""Run the checksum-verified metrics collector without backdating source clocks.

Binance Vision has rare rows whose ``create_time`` is one second after the
nominal five-minute boundary.  That second is information availability, not a
formatting error.  This wrapper reuses the audited collector while replacing
only timestamp parsing: the original UTC instant is preserved exactly and the
existing chronology/gap audit records the irregularity.  No rounding,
forward-fill, credentials, or order endpoints are used.
"""

from __future__ import annotations

from datetime import datetime, timezone

import binance_metrics_bundle as collector


def actual_epoch_ms(raw: str) -> int:
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
        magnitude = abs(value)
        if magnitude < 10**11:
            value *= 1000
        elif magnitude >= 10**15:
            value //= 1000
    if value < 10**12 or value > 10**14:
        raise ValueError(f"implausible metrics timestamp: {raw}")
    return value


collector.epoch_ms = actual_epoch_ms

if __name__ == "__main__":
    raise SystemExit(collector.main())
