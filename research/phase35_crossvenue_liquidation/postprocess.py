from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path('research/phase35_crossvenue_liquidation/results')
COSTS = (8.0, 12.0, 16.0)


def trim(values: np.ndarray, n: int) -> float:
    return float(np.sort(values)[:-n].mean()) if len(values) > n else math.nan


def execute(candidates: pd.DataFrame, books: dict[str, pd.DataFrame], cost: float) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame(columns=['decision_sec','entry_sec','exit_sec','net_bps'])
    rows = []
    free = -10**30
    ordered = candidates.sort_values(['decision_sec','score'], ascending=[True,False])
    for decision_sec, group in ordered.groupby('decision_sec', sort=True):
        t = int(decision_sec)
        if t < free:
            continue
        # Only candidates actually available at this decision second compete.
        for row in group.itertuples(index=False):
            book = books.get(str(row.target_venue))
            if book is None:
                continue
            entry_sec = t + 1
            exit_sec = entry_sec + int(row.horizon)
            if entry_sec not in book.index or exit_sec not in book.index:
                continue
            entry = book.loc[entry_sec]
            exit_ = book.loc[exit_sec]
            side = int(row.side)
            entry_px = float(entry.ask if side > 0 else entry.bid)
            exit_px = float(exit_.bid if side > 0 else exit_.ask)
            if not np.isfinite(entry_px + exit_px) or entry_px <= 0 or exit_px <= 0:
                continue
            net = side * (exit_px / entry_px - 1.0) * 10_000.0 - cost
            rows.append({**row._asdict(), 'entry_sec':entry_sec, 'exit_sec':exit_sec,
                         'entry_px':entry_px, 'exit_px':exit_px, 'net_bps':net})
            free = exit_sec
            break
    return pd.DataFrame(rows)


def stats(trades: pd.DataFrame, dates: list[str]) -> dict[str, float | int]:
    full = {'n':0,'mean':math.nan,'median':math.nan,'top1':math.nan,'top3':math.nan,
            'top5':math.nan,'bps_day':0.0,'positive_days':0.0}
    if trades.empty or not dates:
        return full
    values = trades.net_bps.to_numpy(float)
    days = pd.to_datetime(trades.decision_sec, unit='s', utc=True).dt.strftime('%Y-%m-%d')
    daily = trades.assign(day=days).groupby('day').net_bps.sum().reindex(dates, fill_value=0.0)
    return {'n':int(len(values)), 'mean':float(values.mean()), 'median':float(np.median(values)),
            'top1':trim(values,1), 'top3':trim(values,3), 'top5':trim(values,5),
            'bps_day':float(values.sum()/len(dates)), 'positive_days':float((daily>0).mean())}


def main() -> None:
    panel_path = OUT / 'PANEL_1S.parquet'
    candidate_path = OUT / 'CANDIDATES.csv'
    if not panel_path.exists() or not candidate_path.exists():
        raise RuntimeError('generator did not produce PANEL_1S.parquet and CANDIDATES.csv')
    panel = pd.read_parquet(panel_path, columns=['sec','exchange','bid','ask','mid'])
    panel = panel.sort_values(['exchange','sec']).drop_duplicates(['exchange','sec'], keep='last')
    books = {venue: group.set_index('sec').sort_index() for venue, group in panel.groupby('exchange')}
    candidates = pd.read_csv(candidate_path)
    dates = sorted(pd.to_datetime(panel.sec, unit='s', utc=True).dt.strftime('%Y-%m-%d').unique())
    n = len(dates)
    cut1 = max(1, n//2)
    cut2 = max(cut1+1, int(n*.75))
    cut2 = min(cut2, n-1) if n > 2 else n
    splits = {'dev':dates[:cut1], 'val':dates[cut1:cut2], 'conf':dates[cut2:]}
    rows = []
    trade_cache: dict[str,pd.DataFrame] = {}
    for policy_id, group in candidates.groupby('policy_id', sort=False):
        family = str(group.family.iloc[0])
        for cost in COSTS:
            trades = execute(group, books, cost)
            if cost == 12.0:
                trade_cache[str(policy_id)] = trades
            rec: dict[str, object] = {'policy_id':policy_id, 'family':family, 'cost':cost}
            decision_dates = pd.to_datetime(trades.decision_sec, unit='s', utc=True).dt.strftime('%Y-%m-%d') if len(trades) else pd.Series(dtype=str)
            for split, split_dates in splits.items():
                subset = trades[decision_dates.isin(split_dates)] if len(trades) else trades
                rec.update({split+'_'+k:v for k,v in stats(subset, split_dates).items()})
            rows.append(rec)
    grid = pd.DataFrame(rows)
    base = grid[grid.cost==12.0].copy()
    stress = grid[grid.cost==16.0][['policy_id','dev_mean','val_mean','dev_top1','val_top1','dev_top3','val_top3']].copy()
    stress = stress.rename(columns={c:'stress_'+c for c in stress.columns if c!='policy_id'})
    joined = base.merge(stress, on='policy_id', how='left')
    strict = joined[(joined.dev_n>=20)&(joined.val_n>=10)&(joined.dev_mean>0)&(joined.val_mean>0)&
                    (joined.dev_top5>0)&(joined.val_top5>0)&
                    (joined.stress_dev_mean>0)&(joined.stress_val_mean>0)&
                    (joined.stress_dev_top3>0)&(joined.stress_val_top3>0)].copy()
    discovery = joined[(joined.dev_n>=4)&(joined.val_n>=2)&(joined.dev_mean>0)&(joined.val_mean>0)&
                       (joined.dev_top1>0)&(joined.val_top1>0)&
                       (joined.stress_dev_mean>0)&(joined.stress_val_mean>0)].copy()
    score_cols = ['dev_mean','val_mean','dev_top1','val_top1','stress_dev_mean','stress_val_mean']
    if len(strict):
        strict['score'] = strict[score_cols].min(axis=1)
        strict = strict.sort_values('score', ascending=False)
    if len(discovery):
        discovery['score'] = discovery[score_cols].min(axis=1)
        discovery = discovery.sort_values('score', ascending=False)
    grid.to_csv(OUT/'GRID.csv', index=False)
    strict.to_csv(OUT/'ROBUST.csv', index=False)
    discovery.to_csv(OUT/'DISCOVERY.csv', index=False)
    selected = None
    if len(discovery):
        selected = discovery.iloc[0].replace({np.nan:None}).to_dict()
        trade_cache[str(selected['policy_id'])].to_csv(OUT/'DISCOVERY_TRADES_12BP.csv', index=False)
    status = 'CANDIDATE' if len(strict) and len(dates)>=30 else ('DISCOVERY_ONLY' if len(discovery) else 'CASH')
    summary = {'dates':dates, 'date_count':len(dates), 'rows_1s':int(len(panel)),
               'candidate_rows':int(len(candidates)), 'policies':int(candidates.policy_id.nunique()),
               'strict_robust_count':int(len(strict)), 'discovery_count':int(len(discovery)),
               'status':status, 'discovery_selected':selected, 'split':splits,
               'timestamp':'exchange timestamp only; historical discovery grade',
               'causality':'current liquidation burst, fixed confirmation delay, next-second executable touch, no future cluster maximum',
               'promotion_allowed':False,
               'promotion_reason':'requires at least 30 independent days, forward local-receive-time data, private executions, shadow and paper evidence'}
    (OUT/'SUMMARY.json').write_text(json.dumps(summary, indent=2, sort_keys=True, default=str)+'\n')
    print(json.dumps({k:summary[k] for k in ('date_count','candidate_rows','policies','strict_robust_count','discovery_count','status')}, indent=2))

if __name__ == '__main__':
    main()
