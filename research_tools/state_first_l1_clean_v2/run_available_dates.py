#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('state_first_l1_run_fixed', HERE / 'run_fixed.py')
if SPEC is None or SPEC.loader is None:
    raise ImportError(HERE / 'run_fixed.py')
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)

# A002: changed solely for official daily bookTicker source availability,
# before any candidate PnL, selection, validation or test outcome was observed.
RUN.impl.DAYS = ('2023-06-27', '2023-08-30', '2023-10-25', '2023-12-28')

if __name__ == '__main__':
    raise SystemExit(RUN.impl.main())
