# BTC state / order-flow walk-forward preregistration — 2026-07-24

## Safety boundary

This branch is research-only. It never submits paper, testnet, or live orders; never reads exchange credentials; and never opens 2026 market data. It does not modify the production/default branch.

## Immutable source

- Dataset: `ibrahimdaud/binance-btcusdt`
- Revision: `39b19d4b296129ce5ee1e2118d2ca1c8a49c1984`
- Allowed files: BTCUSDT feature bars and raw Binance USD-M 5-minute klines for 2022–2025 only
- Every downloaded file is SHA-256 hashed before research begins.
- The dataset-provided forward-return columns are not loaded. Executable labels are rebuilt from future raw opens only after model features have been fixed.

## Information timing

- Signals use completed 5-minute bars only.
- OI and positioning snapshots are delayed by one additional completed bar.
- Rolling normalization excludes the current observation from its reference mean and variance.
- A full completed-bar latency is imposed after the signal; entry is at the following bar open after that latency.
- Training labels whose exit crosses a stage boundary are purged.

## Frozen stages

1. Train through 2023-06-30; development evaluation on 2023-H2.
2. Refit the same candidate definitions through 2023-12-31; selection on 2024.
3. Refit the single selected primary through 2024-12-31; one confirmation evaluation on 2025.
4. 2026 remains unopened.

Each later stage requires the same script SHA, source revision, source-file manifest digest, and predecessor content digest.

## Independent model families

- regularized linear return model
- histogram gradient-boosted return model
- three-class cost-exceedance classifier
- separate direction and move-size models
- extremely randomized tree return model
- unsupervised state clustering with shrunk state returns
- fixed state-continuation, absorption-reversal, and crowding-unwind rules

The target 1% geometric daily return is never used for model fitting, parameter ranking, or leverage selection.

## Shared execution model

- global maximum of one BTC position
- risk per trade: 0.5% of current realized strategy equity
- maximum notional leverage: 3×
- capacity: 0.1% of the prior completed five-minute quote volume
- ATR invalidation stops; optional predeclared 3R target
- stop wins an ambiguous same-bar stop/target collision
- gap exits use the adverse executable open
- fees, spread/slippage, market impact, stop slippage, and conservative funding are charged
- base, 1.5×-like, and 2×-like cost environments are evaluated
- UTC daily mark-to-market equity includes every calendar day, including flat days

## Promotion rule

Development representatives must survive multiple cost environments, minimum trade count, drawdown, profit-factor, monthly breadth, and concentration gates. Selection may choose at most three diverse representatives and one primary. Confirmation evaluates only that primary. A deployable package is not created unless the independently confirmed base result reaches at least 1% geometric daily return and all other project gates are satisfied.
