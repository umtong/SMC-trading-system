from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from research.wave39.wave39_engine import sha256_file


def quarter_edges_ms(year: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        (
            int(pd.Timestamp(f"{year}-{start_month:02d}-01T00:00:00Z").timestamp() * 1000),
            int(
                (
                    pd.Timestamp(f"{year + (1 if end_month == 13 else 0)}-"
                                 f"{1 if end_month == 13 else end_month:02d}-01T00:00:00Z")
                ).timestamp() * 1000
            ),
        )
        for start_month, end_month in ((1, 4), (4, 7), (7, 10), (10, 13))
    )


def year_edges_ms(year: int) -> tuple[int, int]:
    return (
        int(pd.Timestamp(f"{year}-01-01T00:00:00Z").timestamp() * 1000),
        int(pd.Timestamp(f"{year + 1}-01-01T00:00:00Z").timestamp() * 1000),
    )


def concatenate_boundary(
    roots_by_year: dict[int, Path],
    symbol: str,
) -> pd.DataFrame:
    frames = []
    for year, root in sorted(roots_by_year.items()):
        path = root / f"{symbol}_quarterhour_exact_{year}.csv.gz"
        frame = pd.read_csv(path)
        if len(frame) != (366 if pd.Timestamp(f"{year}-12-31").is_leap_year else 365) * 96:
            raise RuntimeError(f"{symbol}/{year}: boundary row count mismatch")
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True)
    clock = result["boundary_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(clock) != 900_000):
        raise RuntimeError(f"{symbol}: multiyear boundary clock is not continuous")
    return result


def concatenate_support(
    roots_by_year: dict[int, Path],
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    contract_frames = []
    funding_frames = []
    for year, root in sorted(roots_by_year.items()):
        contract_frames.append(
            pd.read_csv(root / "support" / f"{symbol}_contract_1m_{year}.csv.gz")
        )
        funding_frames.append(
            pd.read_csv(root / "support" / f"{symbol}_funding_{year}.csv.gz")
        )
    contract = pd.concat(contract_frames, ignore_index=True)
    clock = contract["open_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(clock) != 60_000):
        raise RuntimeError(f"{symbol}: multiyear support clock is not continuous")
    funding = pd.concat(funding_frames, ignore_index=True)
    funding.sort_values("funding_time_ms", inplace=True, kind="mergesort")
    duplicate = funding.duplicated("funding_time_ms", keep=False)
    if duplicate.any():
        grouped = funding.loc[duplicate].groupby("funding_time_ms")["funding_rate"].nunique()
        if (grouped > 1).any():
            raise RuntimeError(f"{symbol}: conflicting duplicated funding rows")
    funding.drop_duplicates("funding_time_ms", keep="last", inplace=True)
    funding.reset_index(drop=True, inplace=True)
    return contract, funding


def input_hashes(
    roots_by_year: dict[int, Path],
    symbols: Iterable[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for year, root in sorted(roots_by_year.items()):
        for symbol in symbols:
            for relative in (
                Path(f"{symbol}_quarterhour_exact_{year}.csv.gz"),
                Path("support") / f"{symbol}_contract_1m_{year}.csv.gz",
                Path("support") / f"{symbol}_funding_{year}.csv.gz",
            ):
                path = root / relative
                result[f"{year}/{relative}"] = sha256_file(path)
        for relative in (Path("manifest.json"), Path("support") / "manifest.json"):
            path = root / relative
            result[f"{year}/{relative}"] = sha256_file(path)
    return result


def authorize_2024_candidate(
    *,
    director_registration: Path,
    wave: int,
    candidate_id: str,
    freeze_path: Path,
    result_2023_path: Path,
) -> dict:
    registration = json.loads(director_registration.read_text(encoding="utf-8"))
    if registration.get("schema") != "wave39-41-2024-preaccess-registration-v1":
        raise RuntimeError("director registration schema mismatch")
    if registration.get("registered_before_2024_strategy_outcomes") is not True:
        raise RuntimeError("2024 registration chronology is not valid")
    if registration.get("risk_or_leverage_optimization_allowed") is not False:
        raise RuntimeError("risk or leverage optimization was unexpectedly authorized")
    matches = [
        item for item in registration.get("eligible_candidates", [])
        if int(item.get("wave")) == int(wave) and item.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Wave{wave}/{candidate_id} is not uniquely authorized for 2024")
    authorization = matches[0]
    if authorization.get("freeze_sha256") != sha256_file(freeze_path):
        raise RuntimeError("freeze hash differs from the preaccess registration")
    if authorization.get("result_2023_sha256") != sha256_file(result_2023_path):
        raise RuntimeError("2023 result hash differs from the preaccess registration")
    return registration


def common_2024_gate(
    cost_metrics: dict[str, dict],
    latency2_metrics: dict,
    opposite_metrics: dict,
) -> tuple[bool, dict[str, bool]]:
    m24 = cost_metrics["24"]
    m32 = cost_metrics["32"]
    checks = {
        "minimum_completed_trades": int(m24["trades"]) >= 80,
        "positive_net_log_growth_24bp": float(m24["net_log_growth"]) > 0.0,
        "positive_net_log_growth_32bp": float(m32["net_log_growth"]) > 0.0,
        "positive_quarters_24bp": int(m24["positive_folds"]) >= 3,
        "positive_months_24bp": int(m24["positive_months"]) >= 8,
        "profit_factor_32bp": float(m32["profit_factor"]) >= 1.10,
        "net_after_top5_24bp": float(m24["net_after_top5"]) > 0.0,
        "latency2_net_growth_24bp": float(latency2_metrics["net_log_growth"]) > 0.0,
        "opposite_direction_control_negative_24bp": float(opposite_metrics["net_log_growth"]) < 0.0,
    }
    return bool(all(checks.values())), checks
