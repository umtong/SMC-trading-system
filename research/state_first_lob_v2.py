from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = 'https://data.binance.vision/data/futures/um/daily'
DAYS = ('2023-05-16', '2023-06-10', '2023-08-18', '2023-11-09')
HORIZONS = (3, 10, 30, 60)
QUANTILES = (.95, .975, .99, .995, .999)
COSTS = (.0012, .0018, .0024)
FEATURES = (
    'spread_rel', 'l1_imb', 'micro_dev', 'quote_age_ms', 'log_depth', 'depth_z',
    'spread_z', 'flow_imb_1', 'flow_imb_5', 'flow_imb_30', 'flow_accel',
    'volume_z', 'count_z', 'buy_vwap_dev', 'sell_vwap_dev', 'ret_1', 'ret_5',
    'ret_30', 'rv_30', 'flow_price_eff', 'flow_depth_interaction', 'is_eth',
)


def get(url: str, attempts: int = 6) -> bytes:
    last = None
    for k in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'smc-state-first-l1-v2/1.0'})
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if k + 1 < attempts:
                time.sleep(min(20, 2 ** k))
    raise RuntimeError(f'{url}: {last!r}')


def verified(symbol: str, kind: str, day: str) -> tuple[bytes, dict]:
    name = f'{symbol}-{kind}-{day}.zip'
    url = f'{ROOT}/{kind}/{symbol}/{name}'
    check = get(url + '.CHECKSUM').decode('utf-8-sig').strip()
    expected = check.split()[0].lower()
    payload = get(url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f'checksum mismatch {name}: {actual} != {expected}')
    return payload, {'url': url, 'sha256': actual, 'bytes': len(payload)}


def norm_time(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.int64)
    return np.where(np.abs(a) >= 10**15, a // 1000, a).astype(np.int64)


def read_book(payload: bytes) -> pd.DataFrame:
    use = ['best_bid_price', 'best_bid_qty', 'best_ask_price', 'best_ask_qty', 'event_time']
    parts = []
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        member = [n for n in z.namelist() if n.endswith('.csv')][0]
        for c in pd.read_csv(z.open(member), usecols=use, chunksize=1_000_000):
            c.event_time = norm_time(pd.to_numeric(c.event_time, errors='raise').to_numpy(np.int64))
            for x in use[:-1]:
                c[x] = pd.to_numeric(c[x], errors='raise')
            parts.append(c)
    d = pd.concat(parts, ignore_index=True)
    return d.sort_values('event_time', kind='mergesort').drop_duplicates('event_time', keep='last')


def read_trade(payload: bytes) -> pd.DataFrame:
    use = ['price', 'quantity', 'transact_time', 'is_buyer_maker']
    parts = []
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        member = [n for n in z.namelist() if n.endswith('.csv')][0]
        for c in pd.read_csv(z.open(member), usecols=use, chunksize=1_000_000):
            p = pd.to_numeric(c.price, errors='raise').to_numpy(float)
            q = pd.to_numeric(c.quantity, errors='raise').to_numpy(float)
            t = norm_time(pd.to_numeric(c.transact_time, errors='raise').to_numpy(np.int64))
            sec = t // 1000
            value = p * q
            maker = c.is_buyer_maker.astype(str).str.lower().isin(['true', '1']).to_numpy()
            buy = ~maker
            x = pd.DataFrame({
                'sec': sec, 'quote': value, 'signed': np.where(buy, value, -value),
                'buyq': np.where(buy, value, 0.), 'sellq': np.where(buy, 0., value),
                'buypxq': np.where(buy, p * value, 0.), 'sellpxq': np.where(buy, 0., p * value),
                'count': 1,
            })
            parts.append(x.groupby('sec', sort=False).sum())
    return pd.concat(parts).groupby(level=0).sum().sort_index()


def prior_z(s: pd.Series, w: int = 600, minp: int = 300) -> pd.Series:
    r = s.rolling(w, min_periods=minp)
    return (s - r.mean().shift(1)) / r.std(ddof=0).shift(1).replace(0, np.nan)


def build_panel(symbol: str, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_records = []
    frames = []
    for day in DAYS:
        bp, bm = verified(symbol, 'bookTicker', day)
        tp, tm = verified(symbol, 'aggTrades', day)
        b = read_book(bp)
        t = read_trade(tp)
        bt = b.event_time.to_numpy(np.int64)
        bid = b.best_bid_price.to_numpy(float)
        ask = b.best_ask_price.to_numpy(float)
        bq = b.best_bid_qty.to_numpy(float)
        aq = b.best_ask_qty.to_numpy(float)
        start = max(int(bt.min() // 1000), int(t.index.min()))
        end = min(int(bt.max() // 1000), int(t.index.max()))
        sec = np.arange(start + 1, end - 61, dtype=np.int64)
        decision_ms = (sec + 1) * 1000 - 1
        qi = np.searchsorted(bt, decision_ms, side='right') - 1
        valid = qi >= 0
        sec, decision_ms, qi = sec[valid], decision_ms[valid], qi[valid]
        x = pd.DataFrame(index=sec)
        x['decision_ms'] = decision_ms
        x['bid'], x['ask'], x['bq'], x['aq'] = bid[qi], ask[qi], bq[qi], aq[qi]
        x['quote_age_ms'] = decision_ms - bt[qi]
        tr = t.reindex(sec).fillna(0.)
        x = x.join(tr)
        mid = (x.bid + x.ask) / 2
        total = x.bq + x.aq
        x['spread_rel'] = (x.ask - x.bid) / mid
        x['l1_imb'] = (x.bq - x.aq) / total.replace(0, np.nan)
        x['micro_dev'] = ((x.ask * x.bq + x.bid * x.aq) / total.replace(0, np.nan) - mid) / mid
        x['log_depth'] = np.log1p(total)
        x['depth_z'] = prior_z(x.log_depth)
        x['spread_z'] = prior_z(np.log(x.spread_rel.replace(0, np.nan)))
        x['flow_imb_1'] = x.signed / x.quote.replace(0, np.nan)
        for w in (5, 30):
            x[f'flow_imb_{w}'] = x.signed.rolling(w, min_periods=max(2, w // 2)).sum() / x.quote.rolling(w, min_periods=max(2, w // 2)).sum().replace(0, np.nan)
        x['flow_accel'] = x.flow_imb_5 - x.flow_imb_30
        x['volume_z'] = prior_z(np.log1p(x.quote))
        x['count_z'] = prior_z(np.log1p(x['count']))
        buy_vwap = x.buypxq / x.buyq.replace(0, np.nan)
        sell_vwap = x.sellpxq / x.sellq.replace(0, np.nan)
        x['buy_vwap_dev'] = (buy_vwap - mid) / mid
        x['sell_vwap_dev'] = (sell_vwap - mid) / mid
        x['ret_1'] = np.log(mid / mid.shift(1))
        x['ret_5'] = np.log(mid / mid.shift(5))
        x['ret_30'] = np.log(mid / mid.shift(30))
        x['rv_30'] = x.ret_1.rolling(30, min_periods=15).std(ddof=0).shift(1)
        x['flow_price_eff'] = x.ret_5 / (x.flow_imb_5.abs() + .05)
        x['flow_depth_interaction'] = x.flow_imb_5 * x.l1_imb
        ei = np.searchsorted(bt, decision_ms, side='right')
        ok = ei < len(bt)
        x = x.iloc[np.flatnonzero(ok)].copy()
        ei = ei[ok]
        x['entry_ms'] = bt[ei]
        x['entry_bid'] = bid[ei]
        x['entry_ask'] = ask[ei]
        for h in HORIZONS:
            target_ms = x.entry_ms.to_numpy(np.int64) + h * 1000
            xi = np.searchsorted(bt, target_ms, side='left')
            good = xi < len(bt)
            eb = np.full(len(x), np.nan)
            ea = np.full(len(x), np.nan)
            y = np.full(len(x), np.nan)
            eb[good] = bid[xi[good]]
            ea[good] = ask[xi[good]]
            y[good] = np.log(((bid[xi[good]] + ask[xi[good]]) / 2) / ((x.entry_bid.to_numpy()[good] + x.entry_ask.to_numpy()[good]) / 2))
            x[f'exit_bid_{h}'] = eb
            x[f'exit_ask_{h}'] = ea
            x[f'target_{h}'] = y
        x['symbol'] = symbol
        x['day'] = day
        x['time_ms'] = x.index.to_numpy(np.int64) * 1000
        x['is_eth'] = float(symbol == 'ETHUSDT')
        keep = ['time_ms', 'entry_ms', 'entry_bid', 'entry_ask', 'symbol', 'day', *FEATURES]
        keep += [f'{a}_{h}' for h in HORIZONS for a in ('exit_bid', 'exit_ask', 'target')]
        x = x[keep].replace([np.inf, -np.inf], np.nan).dropna(subset=list(FEATURES) + ['entry_bid', 'entry_ask'])
        frames.append(x)
        source_records.append({'day': day, 'book': bm, 'trades': tm, 'rows': len(x)})
        del bp, tp, b, t, x
    d = pd.concat(frames, ignore_index=True)
    path = output_dir / f'{symbol}_panel.csv.gz'
    d.to_csv(path, index=False, compression={'method': 'gzip', 'compresslevel': 6, 'mtime': 0})
    manifest = {'symbol': symbol, 'days': DAYS, 'rows': len(d), 'sources': source_records, 'output_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'orders_submitted': False}
    (output_dir / f'{symbol}_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def make_model(name: str):
    if name == 'ridge':
        return make_pipeline(SimpleImputer(strategy='median'), StandardScaler(), Ridge(alpha=100.))
    if name == 'hgb':
        return make_pipeline(SimpleImputer(strategy='median'), HistGradientBoostingRegressor(max_iter=200, learning_rate=.04, max_leaf_nodes=15, l2_regularization=20., random_state=2407))
    return make_pipeline(SimpleImputer(strategy='median'), ExtraTreesRegressor(n_estimators=200, max_depth=9, min_samples_leaf=50, max_features=.65, n_jobs=-1, random_state=2407))


def route(d: pd.DataFrame, pred: np.ndarray, h: int, day: str, threshold: float, cost: float) -> pd.DataFrame:
    mask = d.day.eq(day).to_numpy() & np.isfinite(pred) & (np.abs(pred) >= threshold)
    q = d.loc[mask].copy()
    q['pred'] = pred[mask]
    q = q.sort_values(['time_ms', 'pred', 'symbol'], ascending=[True, False, True], key=lambda s: -s.abs() if s.name == 'pred' else s, kind='mergesort')
    rows, free = [], -1
    for t, g in q.groupby('time_ms', sort=True):
        if int(t) < free:
            continue
        r = g.iloc[int(np.argmax(np.abs(g.pred.to_numpy(float))))]
        side = 1 if r.pred > 0 else -1
        ep = r.entry_ask if side > 0 else r.entry_bid
        xp = r[f'exit_bid_{h}'] if side > 0 else r[f'exit_ask_{h}']
        rows.append((int(t), float(side * (xp / ep - 1) - cost), r.symbol))
        free = int(r.entry_ms + h * 1000)
    return pd.DataFrame(rows, columns=['time_ms', 'net', 'symbol'])


def stats(z: pd.DataFrame) -> dict:
    if z.empty:
        return {'n': 0, 'mean_bps': -999., 'trim20_bps': -999., 'pf': 0., 'net': -1., 'top20_conc': 1.}
    v = z.net.to_numpy(float)
    pos = v[v > 0].sum()
    neg = -v[v < 0].sum()
    sv = np.sort(v)
    trim = sv[:-20].mean() if len(v) > 20 else v.mean()
    p = np.sort(v[v > 0])[::-1]
    return {'n': len(v), 'mean_bps': v.mean() * 1e4, 'trim20_bps': trim * 1e4, 'pf': pos / neg if neg else 999., 'net': float(np.prod(1 + v) - 1), 'top20_conc': float(p[:20].sum() / max(p.sum(), 1e-12))}


def evaluate(input_dir: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(input_dir.rglob('*_panel.csv.gz'))
    d = pd.concat([pd.read_csv(p) for p in files], ignore_index=True).sort_values(['time_ms', 'symbol']).reset_index(drop=True)
    rows = []
    train = d.day.eq(DAYS[0])
    selection = d.day.eq(DAYS[1])
    for h in HORIZONS:
        for model_name in ('ridge', 'hgb', 'extra'):
            m = make_model(model_name)
            m.fit(d.loc[train, FEATURES], d.loc[train, f'target_{h}'])
            pred = m.predict(d[list(FEATURES)])
            for qv in QUANTILES:
                threshold = float(np.quantile(np.abs(pred[selection]), qv))
                rec = {'model': model_name, 'horizon': h, 'quantile': qv, 'threshold': threshold}
                for cost in COSTS:
                    for label, day in zip(('selection', 'validation', 'test'), DAYS[1:]):
                        rec.update({f'{label}_{int(cost * 1e4)}bp_{k}': v for k, v in stats(route(d, pred, h, day, threshold, cost)).items()})
                base = int(COSTS[0] * 1e4)
                stress = int(COSTS[1] * 1e4)
                rec['eligible'] = rec[f'selection_{base}bp_n'] >= 100 and rec[f'validation_{base}bp_n'] >= 100 and rec[f'selection_{base}bp_trim20_bps'] > 0 and rec[f'validation_{base}bp_trim20_bps'] > 0 and rec[f'selection_{stress}bp_trim20_bps'] > 0 and rec[f'validation_{stress}bp_trim20_bps'] > 0 and rec[f'selection_{base}bp_pf'] >= 1.1 and rec[f'validation_{base}bp_pf'] >= 1.1 and rec[f'selection_{base}bp_top20_conc'] <= .5 and rec[f'validation_{base}bp_top20_conc'] <= .5
                rows.append(rec)
    screen = pd.DataFrame(rows)
    screen.to_csv(output_dir / 'screen.csv', index=False)
    eligible = screen[screen.eligible]
    target = eligible[(eligible.test_12bp_net >= .01) & (eligible.test_18bp_trim20_bps > 0) & (eligible.test_12bp_n >= 100)]
    summary = {'version': 'STATE_FIRST_L1_V2', 'files': len(files), 'rows': len(d), 'screened': len(screen), 'eligible_pretest': len(eligible), 'target_1pct_final_day': len(target), 'best': screen.sort_values(['eligible', 'validation_12bp_trim20_bps'], ascending=False).head(30).replace([np.nan, np.inf, -np.inf], None).to_dict('records'), 'orders_submitted': False, 'paper_or_live_started': False, 'validated': False}
    (output_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('panel'); p.add_argument('--symbol', required=True); p.add_argument('--output-dir', type=Path, required=True)
    e = sub.add_parser('evaluate'); e.add_argument('--input-dir', type=Path, required=True); e.add_argument('--output-dir', type=Path, required=True)
    args = ap.parse_args()
    if args.command == 'panel':
        result = build_panel(args.symbol.upper(), args.output_dir)
    else:
        result = evaluate(args.input_dir, args.output_dir)
    print(json.dumps(result, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
