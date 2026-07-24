# Liquidation L1 State V1 — data contract

This is a research-only feature extraction stage registered before inspecting L1-conditioned liquidation outcomes.

- Event windows are the canonical 2,964 non-overlapping BTCUSDT/ETHUSDT windows frozen before USD-M outcome ticks were inspected.
- The transmitted payload is repaired only by the previously audited two-character transport contract and must reproduce the canonical payload, gzip, and raw CSV SHA-256 values.
- Official Binance USD-M daily `bookTicker` archives are verified against their adjacent CHECKSUM files.
- Native quote timestamps are normalized row-wise from microseconds to milliseconds only when required by magnitude.
- Every quote update is processed in sequence; OFI, replenishment, and depletion use only the immediately preceding quote.
- Only updates inside frozen half-open windows are retained and aggregated to one-second states.
- A downstream event decision may use only seconds fully completed before its decision boundary. Same-second final BBO values are not presumed known early.
- No price outcome, PnL, future return, credentials, or orders are used by this extraction.

Research only. No paper, testnet, or live orders.
