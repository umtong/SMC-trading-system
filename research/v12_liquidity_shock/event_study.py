from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _global_time_exit_signals(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    selected: list[pd.Series] = []
    occupied_until: pd.Timestamp | None = None
    ordered = signals.sort_values(
        ["entry_time", "score", "symbol"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    for entry_time, group in ordered.groupby("entry_time", sort=True):
        if occupied_until is not None and entry_time < occupied_until:
            continue
        row = group.iloc[0]
        selected.append(row)
        occupied_until = pd.Timestamp(row.exit_time)
    return pd.DataFrame(selected).reset_index(drop=True) if selected else signals.iloc[0:0].copy()


def _hypothesis_masks(frame: pd.DataFrame) -> dict[str, tuple[pd.Series, pd.Series, int]]:
    candle_range = (frame.high - frame.low).replace(0.0, np.nan)
    close_location = ((frame.close - frame.low) / candle_range).clip(0.0, 1.0)
    body_fraction = (frame.close - frame.open).abs() / candle_range
    shock_sign = np.sign(frame.bar_return).astype(int)
    flow_sign = np.sign(frame.taker_imbalance).astype(int)
    basis_sign = np.sign(frame.basis_z).fillna(0).astype(int)

    broad = (
        frame.abs_return_z.ge(3.0)
        & frame.volume_z.ge(2.0)
        & frame.trade_count_z.ge(1.5)
        & frame.taker_imbalance.abs().ge(0.15)
        & shock_sign.ne(0)
    )
    aligned = shock_sign.eq(flow_sign)
    next_basis = frame.basis_z.shift(-1)
    next_return_sign = np.sign(frame.bar_return.shift(-1)).fillna(0).astype(int)
    same_next_segment = frame.segment_id.shift(-1).eq(frame.segment_id)
    basis_contracts = next_basis.abs().le(frame.basis_z.abs() * 0.75)
    basis_reversal = next_return_sign.eq(-basis_sign)

    prior60_high = frame.high.shift(1).rolling(60, min_periods=60).max()
    prior60_low = frame.low.shift(1).rolling(60, min_periods=60).min()
    prior240_high = frame.high.shift(1).rolling(240, min_periods=240).max()
    prior240_low = frame.low.shift(1).rolling(240, min_periods=240).min()
    sweep60_short = frame.high.gt(prior60_high) & frame.close.lt(prior60_high)
    sweep60_long = frame.low.lt(prior60_low) & frame.close.gt(prior60_low)
    sweep240_short = frame.high.gt(prior240_high) & frame.close.lt(prior240_high)
    sweep240_long = frame.low.lt(prior240_low) & frame.close.gt(prior240_low)
    activity = frame.volume_z.ge(1.5) & frame.trade_count_z.ge(1.0)
    absorption_short = flow_sign.gt(0) & close_location.le(0.40) & body_fraction.le(0.50)
    absorption_long = flow_sign.lt(0) & close_location.ge(0.60) & body_fraction.le(0.50)

    def side(long_mask: pd.Series, short_mask: pd.Series) -> pd.Series:
        return pd.Series(np.where(long_mask, 1, np.where(short_mask, -1, 0)), index=frame.index)

    return {
        "shock_continuation": (broad & aligned, pd.Series(shock_sign, index=frame.index), 1),
        "shock_fade": (broad & aligned, pd.Series(-shock_sign, index=frame.index), 1),
        "basis_fade_z2": (broad & frame.basis_z.abs().ge(2.0) & basis_sign.ne(0), pd.Series(-basis_sign, index=frame.index), 1),
        "basis_fade_z3": (broad & frame.basis_z.abs().ge(3.0) & basis_sign.ne(0), pd.Series(-basis_sign, index=frame.index), 1),
        "basis_reversion_confirmed_z2": (
            broad & frame.basis_z.abs().ge(2.0) & basis_sign.ne(0)
            & same_next_segment & basis_contracts & basis_reversal,
            pd.Series(-basis_sign, index=frame.index),
            2,
        ),
        "basis_reversion_confirmed_z3": (
            broad & frame.basis_z.abs().ge(3.0) & basis_sign.ne(0)
            & same_next_segment & basis_contracts & basis_reversal,
            pd.Series(-basis_sign, index=frame.index),
            2,
        ),
        "flow_absorption_fade": (
            activity & frame.taker_imbalance.abs().ge(0.40) & (absorption_long | absorption_short),
            side(absorption_long, absorption_short),
            1,
        ),
        "sweep60_fade": (
            activity & (sweep60_long | sweep60_short),
            side(sweep60_long, sweep60_short),
            1,
        ),
        "sweep240_fade": (
            activity & (sweep240_long | sweep240_short),
            side(sweep240_long, sweep240_short),
            1,
        ),
        "sweep60_flow_absorption": (
            activity & ((sweep60_short & flow_sign.gt(0)) | (sweep60_long & flow_sign.lt(0))),
            side(sweep60_long & flow_sign.lt(0), sweep60_short & flow_sign.gt(0)),
            1,
        ),
        "flow_acceptance_continuation": (
            broad & aligned & body_fraction.ge(0.50)
            & ((shock_sign.gt(0) & close_location.ge(0.75)) | (shock_sign.lt(0) & close_location.le(0.25))),
            pd.Series(shock_sign, index=frame.index),
            1,
        ),
    }


def write_horizon_event_study(
    featured: dict[str, pd.DataFrame],
    *,
    output: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    horizons = (1, 3, 5, 15, 30, 60)
    signal_rows: dict[tuple[str, int], list[dict[str, object]]] = {}
    for symbol, frame in featured.items():
        masks = _hypothesis_masks(frame)
        times = frame.open_time
        for name, (mask, sides, lag) in masks.items():
            for index in np.flatnonzero(mask.fillna(False).to_numpy()):
                signal_side = int(sides.iloc[index])
                if signal_side == 0:
                    continue
                entry_index = index + lag
                if entry_index >= len(frame) or frame.segment_id.iloc[entry_index] != frame.segment_id.iloc[index]:
                    continue
                expected_entry = pd.Timestamp(times.iloc[index]) + pd.Timedelta(minutes=lag)
                if pd.Timestamp(times.iloc[entry_index]) != expected_entry:
                    continue
                entry_price = float(frame.open.iloc[entry_index])
                score = float(np.nansum([
                    abs(float(frame.abs_return_z.iloc[index])),
                    abs(float(frame.volume_z.iloc[index])),
                    abs(float(frame.trade_count_z.iloc[index])),
                    abs(float(frame.basis_z.iloc[index])),
                    abs(float(frame.taker_imbalance.iloc[index])),
                ]))
                for horizon in horizons:
                    exit_index = entry_index + horizon
                    if exit_index >= len(frame) or frame.segment_id.iloc[exit_index] != frame.segment_id.iloc[entry_index]:
                        continue
                    expected_exit = expected_entry + pd.Timedelta(minutes=horizon)
                    if pd.Timestamp(times.iloc[exit_index]) != expected_exit:
                        continue
                    exit_price = float(frame.open.iloc[exit_index])
                    gross_return = signal_side * (exit_price - entry_price) / entry_price
                    signal_rows.setdefault((name, horizon), []).append({
                        "hypothesis": name,
                        "horizon_minutes": horizon,
                        "symbol": symbol,
                        "side": "long" if signal_side > 0 else "short",
                        "signal_time": pd.Timestamp(times.iloc[index]),
                        "entry_time": expected_entry,
                        "exit_time": expected_exit,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "gross_return": gross_return,
                        "score": score,
                    })

    metric_rows: list[dict[str, object]] = []
    ledger_rows: list[pd.DataFrame] = []
    calendar_days = max(1.0, (end - start).total_seconds() / 86_400.0)
    for (name, horizon), raw_rows in sorted(signal_rows.items()):
        selected = _global_time_exit_signals(pd.DataFrame(raw_rows))
        if selected.empty:
            continue
        for cost_bps in (13.0, 20.0, 30.0):
            ledger = selected.copy()
            ledger["cost_bps"] = cost_bps
            ledger["net_return"] = ledger.gross_return - cost_bps / 10_000.0
            ledger["net_bps"] = ledger.net_return * 10_000.0
            ledger["month"] = ledger.exit_time.dt.strftime("%Y-%m")
            month_sum = ledger.groupby("month").net_return.sum()
            symbol_sum = ledger.groupby("symbol").net_return.sum().to_dict()
            metric_rows.append({
                "hypothesis": name,
                "horizon_minutes": horizon,
                "cost_bps": cost_bps,
                "trades": len(ledger),
                "trades_per_calendar_day": len(ledger) / calendar_days,
                "mean_net_bps": float(ledger.net_bps.mean()),
                "median_net_bps": float(ledger.net_bps.median()),
                "win_rate": float(ledger.net_return.gt(0).mean()),
                "total_net_return": float(ledger.net_return.sum()),
                "terminal_multiple_full_notional": float(np.prod(1.0 + ledger.net_return.to_numpy(dtype=float))),
                "positive_month_ratio": float(month_sum.gt(0).mean()) if len(month_sum) else None,
                "BTCUSDT_total_net_return": float(symbol_sum.get("BTCUSDT", 0.0)),
                "ETHUSDT_total_net_return": float(symbol_sum.get("ETHUSDT", 0.0)),
            })
            ledger_rows.append(ledger)
    pd.DataFrame(metric_rows).sort_values(
        ["cost_bps", "hypothesis", "horizon_minutes"]
    ).to_csv(output / "horizon_event_study.csv", index=False)
    if ledger_rows:
        pd.concat(ledger_rows, ignore_index=True).to_csv(output / "horizon_event_ledger.csv", index=False)
