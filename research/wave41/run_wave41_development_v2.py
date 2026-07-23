from __future__ import annotations

import numpy as np
import pandas as pd

from research.wave41 import run_wave41_development as base


def rolling_beta_v2(target: np.ndarray, factor: np.ndarray) -> np.ndarray:
    target_prior = pd.Series(target, dtype="float64").shift(1)
    factor_prior = pd.Series(factor, dtype="float64").shift(1)
    window = target_prior.rolling(60 * 96, min_periods=30 * 96)
    covariance = window.cov(factor_prior)
    variance = factor_prior.rolling(60 * 96, min_periods=30 * 96).var(ddof=0)
    return (covariance / variance.replace(0.0, np.nan)).to_numpy(dtype=np.float64)


base.rolling_beta = rolling_beta_v2


if __name__ == "__main__":
    raise SystemExit(base.main())
