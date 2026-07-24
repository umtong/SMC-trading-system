# V0.27.1 strict evaluation — exact mark-price intersection amendment

Status: `PRE_DENSE_PNL_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

The first visible strict transport reconstructed the sealed runner and produced rolling predictions, but stopped before any dense candidate evaluation because the contract 1-minute grid contained timestamps without an exact official mark-price row.

No candidate dense PnL, family selection, evaluation result, or terminal holdout result was observed before this amendment.

The fixed correction is deliberately conservative:

- contract and official mark-price 1-minute rows are joined only on exactly equal timestamps;
- no forward-fill, backward-fill, interpolation, or contract-price substitution is allowed;
- a signal whose entry, capacity reference, horizon, or any intervening minute is missing is rejected by the sealed dense continuity checks;
- funding rows and every strategy family, feature, model, threshold, base-admitted signal set, cost profile, stop, risk rule, leverage cap, capacity rule, and target gate remain unchanged;
- the terminal holdout remains unopened and production remains disabled.

The amendment changes only unavailable-path handling required to reach the already sealed dense replay. It cannot improve a trade through favorable price imputation because missing paths are removed rather than filled.
