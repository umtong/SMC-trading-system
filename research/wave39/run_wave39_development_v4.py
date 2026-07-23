from __future__ import annotations

from research.wave39 import run_wave39_development as base
from research.wave39 import run_wave39_development_v2 as _flow_flip_patch  # noqa: F401
from research.wave39.wave39_engine_v4 import prior_atr_and_trends, simulate_stop_time_paths

base.prior_atr_and_trends = prior_atr_and_trends
base.simulate_stop_time_paths = simulate_stop_time_paths

if __name__ == "__main__":
    raise SystemExit(base.main())
