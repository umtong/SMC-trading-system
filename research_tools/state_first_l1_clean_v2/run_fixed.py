#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

MODULE_PATH = Path(__file__).with_name('state_first_l1_clean_v2.py')
spec = importlib.util.spec_from_file_location('state_first_l1_clean_v2_impl', MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError(MODULE_PATH)
impl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impl)

_original_build_day = impl.build_day


def _fail_closed_build_day(symbol: str, day: str, data_dir: Path):
    panel, sources = _original_build_day(symbol, day, data_dir)
    valid = panel.entry_time_ms.to_numpy(np.int64) >= 0
    return panel.loc[valid].copy(), sources


impl.build_day = _fail_closed_build_day

if __name__ == '__main__':
    raise SystemExit(impl.main())
