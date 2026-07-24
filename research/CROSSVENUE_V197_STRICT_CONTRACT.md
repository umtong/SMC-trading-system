# V1.97 strict causal cross-venue contract

Registered before any V1.97 result is observed.

## Frozen data split

- 2022-01-15, 03-15, 05-15, 07-15, 09-15, 11-15: development.
- 2023-02-15, 04-15, 06-15, 08-15, 10-15, 12-15: frozen validation.
- 2024 and later are not requested by this workflow.
- BTCUSDT and ETHUSDT share one global pending/open slot.

## Causal decision and execution

- Bybit and Binance public trades are aggregated into completed 100 ms bins.
- A feature may use only bins ending before or at the decision boundary.
- Entry is the first native Binance aggregate trade whose timestamp is **strictly later** than the completed signal-bin boundary.
- Exit is the first native Binance aggregate trade at or after the fixed horizon measured from the actual entry timestamp.
- Global-slot occupancy lasts until that actual exit timestamp, not merely until a nominal horizon.
- Future-return and actual-exit fields are outcome columns only and never signal inputs.

## Frozen hypothesis families

1. Bybit aggressive-flow continuation.
2. Cross-venue flow-gap continuation.
3. Price-gap continuation.
4. Price-gap convergence.
5. Bybit impulse continuation.
6. Bybit-minus-Binance lead-impulse continuation.

Thresholds are `|z| >= 1.5, 2.0, 2.5, 3.0`; confirmation arms are none, price-same, Binance-lag, and flow-same; horizons are 100, 200, 500, 1,000, 2,000, and 5,000 ms. Costs are 4, 6, 8, 12, and 18 bp round trip; promotion requires at least 12 bp.

## Promotion gate before any later date

The same configuration must satisfy both 2022 and 2023:

- at least 100 completed trades per year sample,
- positive net return and profit factor at least 1.10,
- positive return after removing the best 20 trades,
- top-five positive-trade contribution no more than 35% of gross profit,
- at least half of sampled days positive,
- maximum drawdown below 30%.

The 1% geometric daily target is checked only after this gate and is not used to tune thresholds, horizons, or costs.

Research only. No credentials, paper orders, testnet orders, or live orders.
