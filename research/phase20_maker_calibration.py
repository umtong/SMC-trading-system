from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def fill_one(t0, side, limit, queue, ttl_ms, tt, tp, tq, buyer_maker, start):
    end = np.searchsorted(tt, t0 + ttl_ms, side='right')
    cumulative = 0.0
    for j in range(start, end):
        if side > 0 and not buyer_maker[j]:
            continue  # aggressive sell hits bid
        if side < 0 and buyer_maker[j]:
            continue  # aggressive buy lifts ask
        price = tp[j]
        if side > 0:
            if price < limit:
                return j
            if price == limit:
                cumulative += tq[j]
                if cumulative >= queue:
                    return j
        else:
            if price > limit:
                return j
            if price == limit:
                cumulative += tq[j]
                if cumulative >= queue:
                    return j
    return -1


def analyze_day(symbol: str, day: str, root: Path) -> list[dict]:
    book_path = root / f'{symbol}-bookTicker-{day}.zip'
    trade_path = root / f'{symbol}-aggTrades-{day}.zip'
    book = pd.read_csv(
        book_path,
        compression='zip',
        usecols=['best_bid_price', 'best_bid_qty', 'best_ask_price', 'best_ask_qty', 'event_time'],
        dtype={
            'best_bid_price': 'float64', 'best_bid_qty': 'float64',
            'best_ask_price': 'float64', 'best_ask_qty': 'float64',
            'event_time': 'int64',
        },
    ).sort_values('event_time', kind='mergesort')
    trade = pd.read_csv(
        trade_path,
        compression='zip',
        usecols=['price', 'quantity', 'transact_time', 'is_buyer_maker'],
        dtype={'price': 'float64', 'quantity': 'float64', 'transact_time': 'int64', 'is_buyer_maker': 'bool'},
    ).sort_values('transact_time', kind='mergesort')
    bt = book.event_time.to_numpy(np.int64)
    bid = book.best_bid_price.to_numpy(float)
    ask = book.best_ask_price.to_numpy(float)
    bq = book.best_bid_qty.to_numpy(float)
    aq = book.best_ask_qty.to_numpy(float)
    mid = (bid + ask) / 2.0
    tt = trade.transact_time.to_numpy(np.int64)
    tp = trade.price.to_numpy(float)
    tq = trade.quantity.to_numpy(float)
    tm = trade.is_buyer_maker.to_numpy(bool)
    lo = max(int(bt.min()), int(tt.min()))
    hi = min(int(bt.max()), int(tt.max()))
    # One quote opportunity per minute. This samples all sessions without exploding
    # correlated order attempts and keeps the calibration reproducible on hosted CI.
    grid = np.arange((lo // 60_000 + 1) * 60_000, hi, 60_000, dtype=np.int64)
    bi = np.searchsorted(bt, grid, side='right') - 1
    valid = bi >= 0
    grid = grid[valid]
    bi = bi[valid]
    rows = []
    for imbalance_threshold in (0.0, 0.2, 0.4, 0.6):
        for queue_multiplier in (0.5, 1.0, 2.0):
            for ttl_ms in (1_000, 5_000, 30_000):
                observations = []
                for t0, k in zip(grid, bi):
                    total = bq[k] + aq[k]
                    imbalance = (bq[k] - aq[k]) / total if total > 0 else 0.0
                    sides = (1, -1) if imbalance_threshold == 0 else ((1,) if imbalance >= imbalance_threshold else (-1,) if imbalance <= -imbalance_threshold else ())
                    start = np.searchsorted(tt, t0, side='left')
                    for side in sides:
                        limit = bid[k] if side > 0 else ask[k]
                        queue = (bq[k] if side > 0 else aq[k]) * queue_multiplier
                        j = fill_one(t0, side, limit, queue, ttl_ms, tt, tp, tq, tm, start)
                        if j < 0:
                            observations.append((0, np.nan, np.nan, np.nan, side, imbalance))
                            continue
                        fill_time = tt[j]
                        marks = []
                        for lag in (1_000, 5_000, 30_000):
                            z = np.searchsorted(bt, fill_time + lag, side='right') - 1
                            marks.append(side * (mid[z] / limit - 1.0) * 10_000.0 if z >= 0 else np.nan)
                        observations.append((1, *marks, side, imbalance))
                frame = pd.DataFrame(observations, columns=['filled', 'mark1', 'mark5', 'mark30', 'side', 'imbalance'])
                filled = frame[frame.filled == 1]
                rows.append({
                    'symbol': symbol,
                    'day': day,
                    'imbalance_threshold': imbalance_threshold,
                    'queue_multiplier': queue_multiplier,
                    'ttl_ms': ttl_ms,
                    'orders': len(frame),
                    'fills': len(filled),
                    'fill_rate': float(frame.filled.mean()) if len(frame) else np.nan,
                    'markout_1s_bps': float(filled.mark1.mean()) if len(filled) else np.nan,
                    'markout_5s_bps': float(filled.mark5.mean()) if len(filled) else np.nan,
                    'markout_30s_bps': float(filled.mark30.mean()) if len(filled) else np.nan,
                    'net_after_2bp_5s': float(filled.mark5.mean() - 2.0) if len(filled) else np.nan,
                    'net_after_2bp_30s': float(filled.mark30.mean() - 2.0) if len(filled) else np.nan,
                    'start_ms': lo,
                    'end_ms': hi,
                    'book_rows': len(book),
                    'trade_rows': len(trade),
                })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.root / 'MANIFEST.json').read_text())
    days = list(manifest['days'])
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for day in days:
        print('analyze', args.symbol, day, flush=True)
        rows.extend(analyze_day(args.symbol, day, args.root))
    frame = pd.DataFrame(rows)
    frame.to_csv(args.output / f'{args.symbol}_DAILY_QUEUE_CALIBRATION.csv', index=False)
    group = ['imbalance_threshold', 'queue_multiplier', 'ttl_ms']
    aggregate = frame.groupby(group, as_index=False).apply(
        lambda x: pd.Series({
            'days': int(x.day.nunique()),
            'orders': int(x.orders.sum()),
            'fills': int(x.fills.sum()),
            'fill_rate': float(x.fills.sum() / x.orders.sum()) if x.orders.sum() else np.nan,
            'markout_1s_bps': float(np.average(x.markout_1s_bps, weights=np.maximum(x.fills, 1))),
            'markout_5s_bps': float(np.average(x.markout_5s_bps, weights=np.maximum(x.fills, 1))),
            'markout_30s_bps': float(np.average(x.markout_30s_bps, weights=np.maximum(x.fills, 1))),
            'worst_day_net_after_2bp_5s': float(x.net_after_2bp_5s.min()),
            'positive_day_fraction_after_2bp_5s': float((x.net_after_2bp_5s > 0).mean()),
        }),
        include_groups=False,
    ).reset_index(drop=True)
    aggregate.insert(0, 'symbol', args.symbol)
    aggregate.to_csv(args.output / f'{args.symbol}_AGGREGATE_QUEUE_CALIBRATION.csv', index=False)
    summary = {
        'schema_version': 1,
        'symbol': args.symbol,
        'days': days,
        'rows': len(frame),
        'best_aggregate_5s': aggregate.sort_values('markout_5s_bps', ascending=False).head(20).replace({np.nan: None}).to_dict('records'),
        'positive_config_count_after_2bp_5s': int((aggregate.markout_5s_bps > 2.0).sum()),
        'limitations': [
            'top-of-book displayed queue only',
            'one-minute opportunity sampling',
            'no hidden or RPI liquidity',
            'no local order acknowledgement latency',
            'research calibration only',
        ],
    }
    (args.output / f'{args.symbol}_SUMMARY.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps(summary, indent=2)[:12000])


if __name__ == '__main__':
    main()
