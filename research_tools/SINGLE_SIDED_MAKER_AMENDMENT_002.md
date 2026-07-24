# Single-Sided Maker Queue V1 — Amendment 002

Status: `PRE_OUTCOME / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

Before any candidate PnL was observed, two event-ordering issues were removed:

1. a candidate fill could be rejected using the last BBO of the trade's whole second, which may occur after that trade;
2. a passive exit quote could be derived from the last BBO of the fill's whole second, which may occur after the fill.

The fixed contract is:

- order price and displayed queue-ahead come only from the first actual BBO after `known_at + 250 ms`;
- the resting entry remains live for its preregistered lifetime; cancellations ahead are never credited, so displayed queue-ahead is not reduced by unobserved cancellations;
- the first passive exit quote uses the opposite side of the already-known submission BBO;
- stop-first ambiguity is checked at every actual subsequent aggregate trade;
- an unfilled passive exit at the economic horizon is closed at the first actual subsequent aggregate trade, provided it occurs within the fixed 2-second availability bound;
- the dates, candidate rules, policy grid, queue multipliers, risk, leverage cap and cost profiles are unchanged;
- candidate PnL observed before amendment: `false`;
- test opened: `false`;
- paper/live authority: `none`.
