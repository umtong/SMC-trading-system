from __future__ import annotations

# The development panel is the previously examined V0.3-V0.7 benchmark.
DEVELOPMENT_WINDOWS = (
    ("ETHUSDT", "eth_transition", "2024-12-30", "2025-01-13", 0.01),
    ("ETHUSDT", "eth_up", "2025-07-08", "2025-07-22", 0.01),
    ("BTCUSDT", "btc_down_high_vol", "2026-01-23", "2026-02-06", 0.1),
    ("ETHUSDT", "eth_down_high_vol", "2026-01-23", "2026-02-06", 0.01),
    ("BTCUSDT", "btc_up", "2026-04-04", "2026-04-18", 0.1),
    ("BTCUSDT", "btc_range", "2026-06-08", "2026-06-22", 0.1),
)

# Locked before V0.8 execution. Dates are fixed calendar anchors, not selected
# from observed strategy outcomes. BTC and ETH overlap so the global one-slot
# constraint is exercised on every holdout period.
HOLDOUT_WINDOWS = (
    ("BTCUSDT", "holdout_2024_03_btc", "2024-03-01", "2024-03-15", 0.1),
    ("ETHUSDT", "holdout_2024_03_eth", "2024-03-01", "2024-03-15", 0.01),
    ("BTCUSDT", "holdout_2025_03_btc", "2025-03-01", "2025-03-15", 0.1),
    ("ETHUSDT", "holdout_2025_03_eth", "2025-03-01", "2025-03-15", 0.01),
    ("BTCUSDT", "holdout_2025_10_btc", "2025-10-01", "2025-10-15", 0.1),
    ("ETHUSDT", "holdout_2025_10_eth", "2025-10-01", "2025-10-15", 0.01),
    ("BTCUSDT", "holdout_2026_05_btc", "2026-05-01", "2026-05-15", 0.1),
    ("ETHUSDT", "holdout_2026_05_eth", "2026-05-01", "2026-05-15", 0.01),
)

ALL_WINDOWS = (*DEVELOPMENT_WINDOWS, *HOLDOUT_WINDOWS)
