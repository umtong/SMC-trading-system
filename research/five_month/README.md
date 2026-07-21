# Strict Five-Calendar-Month Research

This research track replaces the old full-history gate with the user's actual minimum validation gate:

- each simulation starts with **20,000 USDT total wealth**;
- each simulation spans **exactly five calendar months**;
- BTCUSDT and ETHUSDT share one global pending/open slot;
- all-in stop loss is fixed at **3% of current trading equity**;
- completed trades must exceed the exact number of calendar days in the window;
- the minimum gate is total wealth >= 5x in approximately 99% of frozen holdout windows;
- 5x is a minimum gate, not the optimization objective.

## Causality contract

The assembled script enforces these rules:

1. Only completed M1/M5/H1 bars may enter a decision.
2. H1 values are mapped only after the H1 candle closes.
3. The production candidate set does not use pivots, avoiding hidden future confirmation lag.
4. Post-fill PnL, MFE, MAE, stop/target outcome, and exit data are excluded from entry features.
5. Monthly models train only on orders/trades whose labels finished strictly before that month began.
6. Causal percentile ranks use predictions generated strictly earlier; same-timestamp events do not rank one another.
7. Entry is maker-only with no market fallback. Stop invalidation or +1R departure before fill cancels the order.
8. Official Binance USD-M M1 archives are checksum-verified and M1 resolves fill/stop/target ordering conservatively.

## Assemble locally

```bash
cat research/five_month/parts/part_*.b64 \
  | tr -d '[:space:]' \
  | base64 -d > research/five_month/research_five_month.py

echo '7d8686c9a60c14807a4e1c0b88fbe5c304ea416980fa7cd71227316280803419  research/five_month/research_five_month.py' \
  | sha256sum -c -
```

## Run

```bash
python research/five_month/research_five_month.py \
  --start 2021-01 \
  --end 2026-07 \
  --cache .cache/five_month \
  --output results/five_month_strict \
  --policy-trials 1800 \
  --seed 20260722
```

The workflow caches only checksum-verified official archives. The policy selected on development and validation is frozen before the 2025-2026 holdout is read for promotion decisions. No automatic live promotion is permitted.
