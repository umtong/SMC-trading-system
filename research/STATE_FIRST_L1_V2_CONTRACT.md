# State-First L1 V2 causal research contract

Registered before any V2 multi-day result is observed.

## Frozen market and dates

- Binance USD-M public `bookTicker` and `aggTrades`, checksum verified.
- BTCUSDT and ETHUSDT only; one global pending/open slot.
- Training: 2023-06-15, 2023-07-15, 2023-08-15.
- Calibration: 2023-09-15, 2023-10-15, 2023-11-15.
- Independent validation: 2024-06-15 through 2024-11-15, the 15th of each month.
- 2025 and later are not requested or opened by this workflow.

## Information clock and execution

- Features are formed from a completed one-second interval only.
- Rolling means, deviations, and z-scores use observations strictly before the current value.
- Entry uses the first native best-bid/offer update strictly after the completed second boundary.
- Exit uses the first native best-bid/offer update at or after 3, 10, 30, or 60 seconds from actual entry.
- Long entry crosses at ask and exits at bid; short entry crosses at bid and exits at ask.
- The global slot remains occupied until the actual exit update.
- Future prices, exits, markouts, and PnL are outcome fields only.

## Frozen feature families

- spread and displayed depth state;
- L1 imbalance and microprice deviation;
- quote age and update intensity;
- order-flow imbalance over 1, 5, and 30 seconds;
- bid/ask replenishment and depletion;
- aggressive-flow imbalance and flow relative to displayed depth;
- volume, trade-count, update-count, and volatility state;
- price response per unit of aggressive flow.

## Frozen candidate families

1. Ridge conditional-return model.
2. HistGradientBoosting conditional-return model.
3. Aggressive flow and OFI continuation.
4. Aggressive-flow absorption with opposite-side replenishment.
5. Fragile-liquidity continuation when flow and OFI align while depth is thin or spread is stressed.

Model thresholds are the 90, 95, 97.5, 99, and 99.5 percentiles of absolute training predictions. Costs are 8, 12, and 18 basis points round trip. The target daily return is not used for model fitting, threshold choice, or candidate ranking.

## Promotion gate before 2025

The same configuration at cost of at least 12 bp must satisfy calibration and validation:

- at least 150 and 300 completed trades respectively;
- positive log growth and profit factor at least 1.10;
- top five winners no more than 25% of gross profit;
- at least half of sampled days positive;
- both BTC and ETH represented, with at least 25 each in calibration and 50 each in validation.

Only a passing configuration may justify a separately registered 2025 test. This branch has no credentials and no paper, testnet, or live order authority.
