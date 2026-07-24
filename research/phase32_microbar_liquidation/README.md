# Phase 32 microbar liquidation-depth research

Research-only causal screen over the public `Mindbyte-89/btcusdt-microbar-v2` dataset.

The workflow reconstructs the study script from immutable chunks, downloads trades, top-5 depth and liquidation streams, and evaluates continuation versus reclaim after large forced-liquidation events.

Key causality rules:

- event is the first rising crossing of a development-only liquidation threshold;
- pre-event depth is measured strictly before the event;
- confirmation uses only elapsed post-event seconds;
- entry is at the next observable bid/ask after confirmation;
- one global position slot is enforced;
- development, validation and holdout days are chronological;
- 8/12/18 bp fee stress and top-trade removal are reported.

No production runtime or live order path is modified. Do not merge a candidate based only on this historical dataset because it has exchange timestamps but no local receive timestamps.
