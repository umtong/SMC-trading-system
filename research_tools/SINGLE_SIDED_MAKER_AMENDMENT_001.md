# Single-Sided Maker Queue V1 — Amendment 001

Status: `PRE_OUTCOME / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

Before any candidate PnL or development screen was observed, the first implementation was found to concatenate the decision-second BBO and the BBO resolved after the fixed 250 ms latency using duplicate column names. Pandas could therefore expose the earlier decision BBO to the order simulator.

The correction is mechanical and does not change dates, economic rules, thresholds, queue multipliers, horizons, costs, risk, leverage cap, or validation gates:

- the first actual BBO at or after `known_at + 250 ms` is stored only as `submit_bid`, `submit_ask`, `submit_bid_qty`, and `submit_ask_qty`;
- post-only order price and queue-ahead are derived only from those delayed submission fields;
- the completed decision-second BBO remains a feature-state observation and cannot define the executable order;
- the same admitted fills and signals are replayed under every cost profile;
- candidate PnL observed before amendment: `false`;
- test opened: `false`;
- paper/live authority: `none`.
