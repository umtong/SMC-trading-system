#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np

MODULE = Path(__file__).with_name("liquidation_depth_research_strict.py")
spec = importlib.util.spec_from_file_location("phase26_strict_impl", MODULE)
if spec is None or spec.loader is None:
    raise ImportError(MODULE)
impl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impl)

_original_dumps = json.dumps


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _strict_dumps(value, *args, **kwargs):
    kwargs.pop("allow_nan", None)
    return _original_dumps(_json_safe(value), *args, allow_nan=False, **kwargs)


impl.json.dumps = _strict_dumps

if __name__ == "__main__":
    impl.main()
