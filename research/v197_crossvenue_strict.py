from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE_PATH = Path(__file__).with_name('v196_crossvenue.py')
SPEC = importlib.util.spec_from_file_location('v196_crossvenue_base', BASE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f'cannot load {BASE_PATH}')
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)

BIN_MS = base.BIN_MS
HORIZONS_MS = base.HORIZONS_MS
COSTS_BPS = base.COSTS_BPS
VERSION = 'V1.97_STRICT_CAUSAL_100MS_CROSS_VENUE'


def build_day(symbol: str, day: str, out: Path) -> None:
    braw, binance_meta, binance_time, binance_price = base.parse_binance(symbol, day)
    yraw, bybit_meta = base.parse_bybit(symbol, day)
    start = pd.Timestamp(day, tz='UTC')
    start_ms = int(start.timestamp() * 1_000)
    n = 86_400_000 // BIN_MS
    frame = pd.DataFrame(index=pd.RangeIndex(n, name='bin'))
    frame = frame.join(base._bin_trades(
        braw.time_ms.to_numpy(), braw.price.to_numpy(), braw.quote.to_numpy(),
        braw.signed.to_numpy(), start_ms, n, 'binance_',
    ))
    frame = frame.join(base._bin_trades(
        yraw.time_ms.to_numpy(), yraw.price.to_numpy(), yraw.quote.to_numpy(),
        yraw.signed.to_numpy(), start_ms, n, 'bybit_',
    ))
    for venue in ('binance', 'bybit'):
        frame[f'{venue}_price'] = frame[f'{venue}_price'].ffill()
        for column in ('quote', 'signed', 'count'):
            frame[f'{venue}_{column}'] = frame[f'{venue}_{column}'].fillna(0.0)
    frame = frame.dropna(subset=['binance_price', 'bybit_price']).copy()
    frame['bybit_imb'] = frame.bybit_signed / frame.bybit_quote.replace(0.0, np.nan)
    frame['binance_imb'] = frame.binance_signed / frame.binance_quote.replace(0.0, np.nan)
    frame[['bybit_imb', 'binance_imb']] = frame[['bybit_imb', 'binance_imb']].fillna(0.0)
    frame['flow_gap'] = frame.bybit_imb - frame.binance_imb
    frame['price_gap_bps'] = np.log(frame.bybit_price / frame.binance_price) * 1e4
    for width, label in ((2, '200'), (5, '500'), (10, '1000')):
        frame[f'bybit_ret_{label}_bps'] = np.log(frame.bybit_price / frame.bybit_price.shift(width)) * 1e4
        frame[f'binance_ret_{label}_bps'] = np.log(frame.binance_price / frame.binance_price.shift(width)) * 1e4
        frame[f'lead_ret_{label}_bps'] = frame[f'bybit_ret_{label}_bps'] - frame[f'binance_ret_{label}_bps']
    frame['bybit_flow_z'] = base.causal_z(frame.bybit_imb, 600, 300)
    frame['flow_gap_z'] = base.causal_z(frame.flow_gap, 600, 300)
    frame['price_gap_z'] = base.causal_z(frame.price_gap_bps, 600, 300)
    frame['bybit_ret_z'] = base.causal_z(frame.bybit_ret_500_bps, 600, 300)
    frame['lead_ret_z'] = base.causal_z(frame.lead_ret_500_bps, 600, 300)
    frame['bybit_volume_z'] = base.causal_z(np.log1p(frame.bybit_quote), 600, 300)

    zcols = ['bybit_flow_z', 'flow_gap_z', 'price_gap_z', 'bybit_ret_z', 'lead_ret_z']
    raw_mask = frame[zcols].abs().max(axis=1).ge(1.25) & np.isfinite(frame[zcols]).all(axis=1)
    raw_index = frame.index.to_numpy(np.int64)[raw_mask.to_numpy()]
    keep: list[int] = []
    last = -10_000
    last_sign = 0
    strength = frame.loc[raw_index, zcols].abs().max(axis=1).to_numpy()
    lead_sign = np.sign(frame.loc[raw_index, 'lead_ret_z'].to_numpy())
    for index, sign, value in zip(raw_index, lead_sign, strength):
        if index - last >= 5 or int(sign) != last_sign or value >= 3.0:
            keep.append(int(index))
            last = int(index)
            last_sign = int(sign)
    candidate = frame.loc[keep].copy()

    signal_end_ms = start_ms + (candidate.index.to_numpy(np.int64) + 1) * BIN_MS
    # Strictly later: a trade stamped exactly at the boundary may have contributed
    # to the completed 100 ms state and cannot also be reused as the entry fill.
    entry_index = np.searchsorted(binance_time, signal_end_ms, side='right')
    valid = entry_index < len(binance_time)
    candidate = candidate.iloc[np.flatnonzero(valid)].copy()
    signal_end_ms = signal_end_ms[valid]
    entry_index = entry_index[valid]
    entry_time_ms = binance_time[entry_index]
    entry_price = binance_price[entry_index]
    candidate['signal_end_ms'] = signal_end_ms
    candidate['entry_time_ms'] = entry_time_ms
    candidate['entry_price'] = entry_price
    candidate['entry_delay_ms'] = entry_time_ms - signal_end_ms

    for horizon in HORIZONS_MS:
        target = entry_time_ms + horizon
        exit_index = np.searchsorted(binance_time, target, side='left')
        ok = exit_index < len(binance_time)
        actual_exit_time = np.full(len(candidate), np.nan)
        forward = np.full(len(candidate), np.nan)
        actual_exit_time[ok] = binance_time[exit_index[ok]]
        forward[ok] = np.log(binance_price[exit_index[ok]] / entry_price[ok])
        candidate[f'exit_time_{horizon}ms'] = actual_exit_time
        candidate[f'fwd_{horizon}ms'] = forward

    feature_columns = [
        'signal_end_ms', 'entry_time_ms', 'entry_price', 'entry_delay_ms',
        'bybit_imb', 'binance_imb', 'flow_gap', 'price_gap_bps',
        'bybit_ret_200_bps', 'binance_ret_200_bps', 'lead_ret_200_bps',
        'bybit_ret_500_bps', 'binance_ret_500_bps', 'lead_ret_500_bps',
        'bybit_ret_1000_bps', 'binance_ret_1000_bps', 'lead_ret_1000_bps',
        'bybit_flow_z', 'flow_gap_z', 'price_gap_z', 'bybit_ret_z',
        'lead_ret_z', 'bybit_volume_z',
    ]
    outcome_columns = [
        column
        for horizon in HORIZONS_MS
        for column in (f'exit_time_{horizon}ms', f'fwd_{horizon}ms')
    ]
    candidate = candidate[feature_columns + outcome_columns].replace([np.inf, -np.inf], np.nan).dropna().copy()
    if len(candidate):
        if not (candidate.entry_time_ms > candidate.signal_end_ms).all():
            raise AssertionError('entry must be strictly after the completed signal bin')
        for horizon in HORIZONS_MS:
            if not (candidate[f'exit_time_{horizon}ms'] >= candidate.entry_time_ms + horizon).all():
                raise AssertionError(f'early exit detected at {horizon} ms')
    candidate['symbol'] = symbol
    candidate['day'] = day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{symbol}_{day}_100ms_strict.csv.gz'
    candidate.to_csv(path, index=False, compression={'method': 'gzip', 'compresslevel': 6, 'mtime': 0})
    manifest = {
        'version': VERSION,
        'symbol': symbol,
        'day': day,
        'rows': len(candidate),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'binance': binance_meta,
        'bybit': bybit_meta,
        'decision_bin_ms': BIN_MS,
        'entry': 'first native Binance aggregate trade strictly after completed signal-bin end',
        'exit': 'first native Binance aggregate trade at or after horizon from actual entry',
        'minimum_entry_delay_ms': int(candidate.entry_delay_ms.min()) if len(candidate) else None,
        'future_returns_used_in_signal': False,
        'orders_submitted': False,
    }
    (out / f'{symbol}_{day}_100ms_strict.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest), flush=True)


def greedy_actual(entry_ms: np.ndarray, exit_ms: np.ndarray) -> np.ndarray:
    selected: list[int] = []
    free = np.iinfo(np.int64).min
    for index, (entry, exit_) in enumerate(zip(entry_ms, exit_ms)):
        if entry >= free:
            selected.append(index)
            free = int(exit_)
    return np.asarray(selected, dtype=int)


def trimmed_mean_bps(values: np.ndarray, n: int) -> float:
    if len(values) <= n:
        return float('nan')
    return float(np.sort(values)[:-n].mean() * 1e4)


def metric(returns: np.ndarray, day_labels: np.ndarray, operating_days: int) -> dict[str, float | int]:
    if len(returns) == 0:
        return {
            'trades': 0, 'net_return': 0.0, 'gday': 0.0, 'max_drawdown': 0.0,
            'profit_factor': 0.0, 'avg_net_bps': 0.0, 'top20_bps': np.nan,
            'top5_positive_share': 1.0, 'positive_day_fraction': 0.0,
        }
    equity = np.cumprod(1 + np.maximum(returns, -0.999))
    curve = np.r_[1.0, equity]
    drawdown = 1 - curve / np.maximum.accumulate(curve)
    positive = returns[returns > 0]
    negative = -returns[returns < 0]
    gross_profit = float(positive.sum())
    top5_share = 1.0 if gross_profit <= 0 else float(np.sort(positive)[::-1][:5].sum() / gross_profit)
    day_frame = pd.DataFrame({'day': day_labels, 'return': returns})
    daily = day_frame.groupby('day', sort=True).return.sum()
    return {
        'trades': int(len(returns)),
        'net_return': float(equity[-1] - 1),
        'gday': float(np.exp(np.log(equity[-1]) / operating_days) - 1),
        'max_drawdown': float(drawdown.max()),
        'profit_factor': float(positive.sum() / negative.sum()) if negative.sum() > 0 else 999.0,
        'avg_net_bps': float(returns.mean() * 1e4),
        'top20_bps': trimmed_mean_bps(returns, 20),
        'top5_positive_share': top5_share,
        'positive_day_fraction': float((daily > 0).mean()),
    }


def aggregate(input_dir: Path, output_dir: Path) -> None:
    files = sorted(input_dir.rglob('*_100ms_strict.csv.gz'))
    if len(files) != 24:
        raise ValueError(f'expected 24 symbol-day panels, found {len(files)}')
    frames = [pd.read_csv(path) for path in files]
    panel = pd.concat(frames, ignore_index=True).sort_values(['entry_time_ms', 'symbol'], kind='mergesort')
    panel['year'] = pd.to_datetime(panel.entry_time_ms, unit='ms', utc=True).dt.year
    rules: list[dict[str, object]] = []
    families = ('bybit_flow', 'flow_gap', 'price_gap_cont', 'price_gap_conv', 'bybit_impulse', 'lead_impulse')
    for family in families:
        if family == 'bybit_flow':
            raw = panel.bybit_flow_z.to_numpy(); base_direction = np.sign(raw)
        elif family == 'flow_gap':
            raw = panel.flow_gap_z.to_numpy(); base_direction = np.sign(raw)
        elif family == 'price_gap_cont':
            raw = panel.price_gap_z.to_numpy(); base_direction = np.sign(raw)
        elif family == 'price_gap_conv':
            raw = panel.price_gap_z.to_numpy(); base_direction = -np.sign(raw)
        elif family == 'bybit_impulse':
            raw = panel.bybit_ret_z.to_numpy(); base_direction = np.sign(raw)
        else:
            raw = panel.lead_ret_z.to_numpy(); base_direction = np.sign(raw)
        for threshold_z in (1.5, 2.0, 2.5, 3.0):
            threshold = np.abs(raw) >= threshold_z
            for confirmation in ('none', 'price_same', 'binance_lag', 'flow_same'):
                mask = threshold & (base_direction != 0)
                if confirmation == 'price_same':
                    mask &= np.sign(panel.bybit_ret_500_bps.to_numpy()) == base_direction
                elif confirmation == 'binance_lag':
                    bybit_return = panel.bybit_ret_500_bps.to_numpy()
                    binance_return = panel.binance_ret_500_bps.to_numpy()
                    mask &= (np.sign(bybit_return) == base_direction) & (np.abs(binance_return) <= 0.5 * np.abs(bybit_return))
                elif confirmation == 'flow_same':
                    mask &= np.sign(panel.bybit_imb.to_numpy()) == base_direction
                index = np.flatnonzero(mask)
                if not len(index):
                    continue
                score = np.abs(raw[index]) + np.maximum(base_direction[index] * panel.bybit_ret_z.to_numpy()[index], 0.0)
                order = np.lexsort((panel.symbol.to_numpy()[index], -score, panel.entry_time_ms.to_numpy()[index]))
                index = index[order]
                for horizon in HORIZONS_MS:
                    entry = panel.entry_time_ms.to_numpy(np.int64)[index]
                    exit_ = panel[f'exit_time_{horizon}ms'].to_numpy(np.int64)[index]
                    chosen = greedy_actual(entry, exit_)
                    use = index[chosen]
                    gross = base_direction[use] * panel[f'fwd_{horizon}ms'].to_numpy()[use]
                    years = panel.year.to_numpy()[use]
                    days = panel.day.to_numpy()[use]
                    for cost in COSTS_BPS:
                        net = gross - cost / 1e4
                        record: dict[str, object] = {
                            'config': f'{family}_z{threshold_z}_{confirmation}_h{horizon}_c{int(cost)}',
                            'family': family, 'z': threshold_z, 'confirm': confirmation,
                            'horizon_ms': horizon, 'cost_bps': cost,
                        }
                        for year in (2022, 2023):
                            year_mask = years == year
                            result = metric(net[year_mask], days[year_mask], 6)
                            record.update({f'{key}_{year}': value for key, value in result.items()})
                        rules.append(record)
    screen = pd.DataFrame(rules)
    screen['min_gday'] = screen[['gday_2022', 'gday_2023']].min(axis=1)
    screen['min_trades'] = screen[['trades_2022', 'trades_2023']].min(axis=1)
    screen['max_dd'] = screen[['max_drawdown_2022', 'max_drawdown_2023']].max(axis=1)
    screen['min_top20_bps'] = screen[['top20_bps_2022', 'top20_bps_2023']].min(axis=1)
    screen['max_top5_share'] = screen[['top5_positive_share_2022', 'top5_positive_share_2023']].max(axis=1)
    screen['min_positive_day_fraction'] = screen[['positive_day_fraction_2022', 'positive_day_fraction_2023']].min(axis=1)
    screen = screen.sort_values(['min_gday', 'min_top20_bps', 'max_dd'], ascending=[False, False, True])
    output_dir.mkdir(parents=True, exist_ok=True)
    screen.to_csv(output_dir / 'screen_100ms_strict.csv', index=False)
    eligible = screen[
        (screen.cost_bps >= 12)
        & (screen.min_trades >= 100)
        & (screen.net_return_2022 > 0) & (screen.net_return_2023 > 0)
        & (screen.profit_factor_2022 >= 1.1) & (screen.profit_factor_2023 >= 1.1)
        & (screen.top20_bps_2022 > 0) & (screen.top20_bps_2023 > 0)
        & (screen.max_top5_share <= 0.35)
        & (screen.min_positive_day_fraction >= 0.50)
        & (screen.max_dd < 0.30)
    ]
    eligible.to_csv(output_dir / 'eligible_100ms_strict.csv', index=False)
    target = eligible[(eligible.gday_2022 >= 0.01) & (eligible.gday_2023 >= 0.01)]
    summary = {
        'version': VERSION,
        'files': len(files),
        'rows': len(panel),
        'screened': len(screen),
        'eligible': len(eligible),
        'target_1pct_both_years': len(target),
        'best': screen.head(30).replace([np.inf, -np.inf, np.nan], None).to_dict('records'),
        'later_years_opened': False,
        'orders_submitted': False,
        'paper_or_live_started': False,
    }
    (output_dir / 'summary_100ms_strict.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    day_parser = sub.add_parser('day')
    day_parser.add_argument('--symbol', required=True)
    day_parser.add_argument('--day', required=True)
    day_parser.add_argument('--out', type=Path, required=True)
    aggregate_parser = sub.add_parser('aggregate')
    aggregate_parser.add_argument('--input', type=Path, required=True)
    aggregate_parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'day':
        build_day(args.symbol, args.day, args.out)
    else:
        aggregate(args.input, args.out)


if __name__ == '__main__':
    main()
