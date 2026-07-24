# Phase21 Hyperliquid wallet-flow price-discovery preregistration

Status: `RESEARCH_ONLY / NO_ORDER_AUTHORITY / PRE_OUTCOME_LOCK`

- Public Hyperliquid block/fill data and checksum-verified Binance USD-M aggregate trades only.
- Fixed windows: 2025-07-28, 2025-08-01, 2025-08-08, 2025-08-15, 2025-08-22, 2025-09-01, 2025-09-15, 2025-10-01 at 00/06/12/18 UTC.
- Any wallet reputation or skill score may use only fills, fees and closed PnL timestamped strictly before the decision bucket.
- Current-fill closed PnL, later wallet outcomes, future Binance returns and final position outcomes are prohibited as inputs.
- A signal becomes known only after a completed bucket; Binance execution must use the first trade or BBO strictly after that boundary.
- BTCUSDT and ETHUSDT share one global pending/open slot.
- Candidate results must be replayed under one frozen signal set at base and stressed costs.
- No paper, testnet or live order is allowed; 1% net geometric daily and all stability gates remain mandatory before any deployable bundle.
