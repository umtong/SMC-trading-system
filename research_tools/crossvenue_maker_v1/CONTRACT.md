# Cross-Venue Maker V1 — frozen causal contract

Registered before any result is observed.

## Frozen data
- Development: BTCUSDT and ETHUSDT on 2022-01-15, 03-15, 05-15, 07-15, 09-15, 11-15.
- Independent validation: BTCUSDT and ETHUSDT on 2023-02-15, 04-15, 06-15, 08-15, 10-15, 12-15.
- 2024 and later are not requested.

## Information clock and execution
- Signal features use only completed 100 ms Bybit and Binance aggregate-trade bins.
- A fixed 100 ms acknowledgement delay is applied.
- The posted price and displayed queue are the first Binance BBO strictly after signal-end plus acknowledgement delay.
- Buy limits post at bid and can fill only from later aggressive sells; sell limits post at ask and can fill only from later aggressive buys.
- At the same price, displayed queue times the fixed multiplier must trade before our order. A trade through the limit fills conservatively at our limit.
- Pending orders occupy the one global BTC/ETH slot until fill or cancel. Filled positions occupy it until the actual native BBO exit update.
- Exit is taker at bid for longs and ask for shorts after the fixed post-fill horizon.

## Frozen mechanisms and grid
- Bybit flow, cross-venue flow gap, price-gap continuation, price-gap convergence, Bybit impulse, and Bybit-minus-Binance lead impulse.
- |z| thresholds: 1.5, 2.0, 2.5.
- Confirmations: none or Binance lag.
- Queue multipliers: 0.5 and 1.0.
- TTL: 500, 2,000, and 5,000 ms.
- Post-fill horizons: 1,000, 2,000, 5,000, and 10,000 ms.
- Round-trip all-in cost stress: 8, 12, and 16 bp. Promotion requires the same structure to pass both 8 and 12 bp.

## Promotion
Both 2022 and 2023, at base and stress costs:
- at least 100 fills,
- positive net return and PF >= 1.10,
- positive mean after removing the best 20 trades,
- top-five profit share <= 35%,
- at least half of all six sampled days positive, including zero-trade days,
- at least 20 fills in each symbol,
- MDD < 30%.

The one-percent daily target is reported after the gate and never used to choose the grid.

Research only. No credentials or orders.
