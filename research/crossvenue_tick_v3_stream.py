#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

BINANCE = 'https://data.binance.vision/data/futures/um/daily/aggTrades'
BYBIT = 'https://public.bybit.com/trading'
SYMBOLS = ('BTCUSDT', 'ETHUSDT')
DEV_DAYS = ('2022-01-15','2022-03-15','2022-05-15','2022-07-15','2022-09-15','2022-11-15')
VAL_DAYS = ('2023-01-15','2023-03-15','2023-05-15','2023-07-15','2023-09-15','2023-11-15')
FINAL_DAYS = ('2024-01-15','2024-03-15','2024-05-15','2024-07-15','2024-09-15','2024-11-15')
RULES = ('price_under','flow_confirm','flow_diverge_revert','strict_under')
WINDOWS = (500, 1000)
ZS = (1.5, 2.0, 2.5, 3.0)
LATENCIES = (100, 250, 500, 1000)
HORIZONS = (2_000, 5_000, 15_000, 30_000)
COSTS_BPS = (12.0, 18.0, 24.0)
BIN_MS = 100
GRID_SIZE = 24 * 60 * 60 * 10
ENTRY_MAX_DELAY_MS = 2_000
EXIT_MAX_DELAY_MS = 2_000
CHUNK_ROWS = 500_000
USER_AGENT = 'smc-crossvenue-v3-stream/1.0'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def download(url: str, path: Path, attempts: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=600) as response, path.open('wb') as output:
                shutil.copyfileobj(response, output, length=1 << 20)
            return
        except Exception as exc:
            error = exc
            path.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f'download failed {url}: {error!r}')


def ensure_binance(cache: Path, symbol: str, day: str) -> tuple[Path, dict[str, object]]:
    name = f'{symbol}-aggTrades-{day}.zip'
    url = f'{BINANCE}/{symbol}/{name}'
    path = cache / name
    checksum_path = cache / f'{name}.CHECKSUM'
    if not checksum_path.exists():
        download(url + '.CHECKSUM', checksum_path)
    expected = checksum_path.read_text(encoding='utf-8-sig').strip().split()[0].lower()
    if not path.exists() or sha256(path) != expected:
        download(url, path)
    observed = sha256(path)
    if observed != expected:
        raise ValueError(f'checksum mismatch {name}: {observed} != {expected}')
    return path, {'venue':'binance','url':url,'sha256':observed,'bytes':path.stat().st_size}


def ensure_bybit(cache: Path, symbol: str, day: str) -> tuple[Path, dict[str, object]]:
    name = f'{symbol}{day}.csv.gz'
    url = f'{BYBIT}/{symbol}/{name}'
    path = cache / name
    if not path.exists():
        download(url, path)
    return path, {'venue':'bybit','url':url,'sha256':sha256(path),'bytes':path.stat().st_size}


def normalize_ms(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    if not len(finite):
        return np.empty(0, dtype=np.int64)
    median = float(np.median(np.abs(finite)))
    if median < 1e11:
        values = values * 1000.0
    elif median >= 1e17:
        values = np.floor(values / 1_000_000.0)
    elif median >= 1e14:
        values = np.floor(values / 1_000.0)
    return values.astype(np.int64)


def _binance_chunks(path: Path) -> Iterable[tuple[np.ndarray,np.ndarray,np.ndarray,np.ndarray]]:
    names = ('agg','price','qty','first','last','time','maker')
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(members) != 1:
            raise ValueError(f'unexpected Binance CSV members: {members}')
        with archive.open(members[0]) as raw:
            reader = pd.read_csv(raw, header=None, names=names, usecols=['price','qty','time','maker'], chunksize=CHUNK_ROWS, low_memory=False)
            for chunk in reader:
                numeric_time = pd.to_numeric(chunk['time'], errors='coerce')
                good = numeric_time.notna()
                if not bool(good.any()):
                    continue
                chunk = chunk.loc[good]
                t = normalize_ms(numeric_time.loc[good].to_numpy())
                p = pd.to_numeric(chunk['price'], errors='raise').to_numpy(float)
                q = pd.to_numeric(chunk['qty'], errors='raise').to_numpy(float)
                maker = chunk['maker'].astype(str).str.strip().str.lower().isin(('true','1')).to_numpy(bool)
                order = np.argsort(t, kind='stable')
                yield t[order], p[order], q[order], ~maker[order]


def _bybit_columns(path: Path) -> dict[str,str]:
    with gzip.open(path, 'rt', encoding='utf-8-sig', newline='') as handle:
        header = handle.readline().strip().split(',')
    mapping = {name.strip().lower(): name.strip() for name in header}
    aliases = {
        'time': ('timestamp','time','trade_time_ms'),
        'price': ('price','trade_price'),
        'qty': ('size','qty','quantity'),
        'side': ('side','takerside'),
    }
    result: dict[str,str] = {}
    for key, options in aliases.items():
        match = next((mapping[name] for name in options if name in mapping), None)
        if match is None:
            raise ValueError(f'Bybit column {key} missing from {header}')
        result[key] = match
    return result


def _bybit_chunks(path: Path) -> Iterable[tuple[np.ndarray,np.ndarray,np.ndarray,np.ndarray]]:
    columns = _bybit_columns(path)
    usecols = list(columns.values())
    for chunk in pd.read_csv(path, compression='gzip', usecols=usecols, chunksize=CHUNK_ROWS, low_memory=False):
        t = normalize_ms(pd.to_numeric(chunk[columns['time']], errors='raise').to_numpy())
        p = pd.to_numeric(chunk[columns['price']], errors='raise').to_numpy(float)
        q = pd.to_numeric(chunk[columns['qty']], errors='raise').to_numpy(float)
        buy = chunk[columns['side']].astype(str).str.strip().str.lower().str.startswith('b').to_numpy(bool)
        order = np.argsort(t, kind='stable')
        yield t[order], p[order], q[order], buy[order]


def aggregate_100ms(chunks: Iterable[tuple[np.ndarray,np.ndarray,np.ndarray,np.ndarray]], day_start_ms: int) -> dict[str,np.ndarray]:
    quote = np.zeros(GRID_SIZE, dtype=np.float64)
    signed = np.zeros(GRID_SIZE, dtype=np.float64)
    count = np.zeros(GRID_SIZE, dtype=np.int32)
    last_time = np.full(GRID_SIZE, -1, dtype=np.int64)
    last_price = np.full(GRID_SIZE, np.nan, dtype=np.float64)
    day_end_ms = day_start_ms + 86_400_000
    source_rows = 0
    accepted_rows = 0
    for t, p, q, buy in chunks:
        source_rows += len(t)
        good = (t >= day_start_ms) & (t < day_end_ms) & np.isfinite(p) & np.isfinite(q) & (p > 0) & (q >= 0)
        if not bool(good.any()):
            continue
        t = t[good]; p = p[good]; q = q[good]; buy = buy[good]
        accepted_rows += len(t)
        idx = ((t - day_start_ms) // BIN_MS).astype(np.int64)
        value = p * q
        np.add.at(quote, idx, value)
        np.add.at(signed, idx, np.where(buy, value, -value))
        np.add.at(count, idx, 1)
        order = np.lexsort((t, idx))
        sorted_idx = idx[order]
        boundaries = np.r_[np.flatnonzero(np.diff(sorted_idx)) + 1, len(sorted_idx)]
        chosen = order[boundaries - 1]
        ci = idx[chosen]
        ct = t[chosen]
        newer = ct >= last_time[ci]
        if bool(newer.any()):
            use = chosen[newer]
            ui = idx[use]
            last_time[ui] = t[use]
            last_price[ui] = p[use]
    return {
        'quote': quote,
        'signed': signed,
        'count': count,
        'last_time': last_time,
        'last_price': last_price,
        'source_rows': np.array([source_rows], dtype=np.int64),
        'accepted_rows': np.array([accepted_rows], dtype=np.int64),
    }


def build_panel(binance: dict[str,np.ndarray], bybit: dict[str,np.ndarray], day_start_ms: int) -> pd.DataFrame:
    b_obs = np.flatnonzero(binance['last_time'] >= 0)
    y_obs = np.flatnonzero(bybit['last_time'] >= 0)
    if not len(b_obs) or not len(y_obs):
        raise ValueError('venue has no accepted trades')
    lo = int(max(b_obs[0], y_obs[0]))
    hi = int(min(b_obs[-1], y_obs[-1]))
    if hi <= lo:
        raise ValueError('no overlapping venue interval')
    index = np.arange(lo, hi + 1, dtype=np.int64)
    frame = pd.DataFrame({'bin_idx': index, 'bin_ms': day_start_ms + index * BIN_MS})
    for prefix, source in (('b',binance),('y',bybit)):
        prices = pd.Series(source['last_price']).ffill().to_numpy()[index]
        q = source['quote'][index]
        s = source['signed'][index]
        frame[f'{prefix}_last'] = prices
        frame[f'{prefix}_quote'] = q
        frame[f'{prefix}_signed'] = s
        frame[f'{prefix}_ret_500'] = np.log(frame[f'{prefix}_last'] / frame[f'{prefix}_last'].shift(5))
        frame[f'{prefix}_ret_1000'] = np.log(frame[f'{prefix}_last'] / frame[f'{prefix}_last'].shift(10))
        for width, bins in ((500,5),(1000,10)):
            frame[f'{prefix}_flow_{width}'] = (
                frame[f'{prefix}_signed'].rolling(bins, min_periods=max(3,bins//2)).sum()
                / frame[f'{prefix}_quote'].rolling(bins, min_periods=max(3,bins//2)).sum().replace(0,np.nan)
            )
    for column in ('y_ret_500','y_ret_1000','y_flow_500','y_flow_1000'):
        rolling = frame[column].rolling(18_000, min_periods=3_600)
        frame[column + '_z'] = (frame[column] - rolling.mean().shift(1)) / rolling.std(ddof=0).shift(1).replace(0,np.nan)
    frame['under_500'] = frame.y_ret_500 - frame.b_ret_500
    frame['under_1000'] = frame.y_ret_1000 - frame.b_ret_1000
    return frame.replace([np.inf,-np.inf],np.nan)


def signal_indices(frame: pd.DataFrame) -> dict[tuple[str,int,float],np.ndarray]:
    output: dict[tuple[str,int,float],np.ndarray] = {}
    for window in WINDOWS:
        suffix = str(window)
        shock = frame[f'y_ret_{suffix}_z'].to_numpy(float)
        flow = frame[f'y_flow_{suffix}_z'].to_numpy(float)
        under = frame[f'under_{suffix}'].to_numpy(float)
        bret = frame[f'b_ret_{suffix}'].to_numpy(float)
        yret = frame[f'y_ret_{suffix}'].to_numpy(float)
        for threshold in ZS:
            price_finite = np.isfinite(shock) & np.isfinite(under)
            flow_finite = np.isfinite(shock) & np.isfinite(flow)
            strict_finite = np.isfinite(shock) & np.isfinite(bret) & np.isfinite(yret)
            masks = {
                'price_under': price_finite & (np.abs(shock) >= threshold) & (np.sign(shock) == np.sign(under)),
                'flow_confirm': flow_finite & (np.abs(shock) >= threshold) & (np.abs(flow) >= threshold) & (np.sign(shock) == np.sign(flow)),
                'flow_diverge_revert': flow_finite & (np.abs(shock) >= threshold) & (np.abs(flow) >= threshold) & (np.sign(shock) != np.sign(flow)),
                'strict_under': strict_finite & (np.abs(shock) >= threshold) & (np.abs(bret) < np.abs(yret) * 0.35),
            }
            for rule, mask in masks.items():
                output[(rule,window,threshold)] = np.flatnonzero(mask)
    return output


def signal_sides(frame: pd.DataFrame, rule: str, window: int, idx: np.ndarray) -> np.ndarray:
    shock = frame[f'y_ret_{window}_z'].to_numpy(float)[idx]
    if rule == 'flow_diverge_revert':
        return -np.sign(shock).astype(np.int8)
    return np.sign(shock).astype(np.int8)


def resolve_binance(path: Path, targets: np.ndarray, max_delay_ms: int) -> tuple[np.ndarray,np.ndarray]:
    unique = np.unique(np.asarray(targets, dtype=np.int64))
    actual_time = np.full(len(unique), -1, dtype=np.int64)
    price = np.full(len(unique), np.nan, dtype=np.float64)
    cursor = 0
    previous = -1
    for t, p, _q, _buy in _binance_chunks(path):
        if len(t) and previous > int(t[0]):
            raise ValueError('Binance aggregate trades are not globally chronological')
        if len(t):
            previous = int(t[-1])
        if cursor >= len(unique) or not len(t):
            continue
        end = int(np.searchsorted(unique, t[-1], side='right'))
        if end <= cursor:
            continue
        query = unique[cursor:end]
        positions = np.searchsorted(t, query, side='left')
        good = positions < len(t)
        rows = np.flatnonzero(good)
        if len(rows):
            chosen = positions[good]
            actual_time[cursor + rows] = t[chosen]
            price[cursor + rows] = p[chosen]
        cursor = end
    delay = actual_time - unique
    bad = (actual_time < 0) | (delay < 0) | (delay > max_delay_ms)
    actual_time[bad] = -1
    price[bad] = np.nan
    return unique, np.column_stack((actual_time, price))


def lookup_resolved(unique: np.ndarray, resolved: np.ndarray, targets: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
    positions = np.searchsorted(unique, targets)
    if np.any(positions >= len(unique)) or np.any(unique[positions] != targets):
        raise AssertionError('target lookup mismatch')
    return resolved[positions,0].astype(np.int64), resolved[positions,1].astype(float)


def candidate_legs(frame: pd.DataFrame, binance_path: Path, symbol: str, day: str) -> dict[tuple[str,int,float,int,int],pd.DataFrame]:
    signals = signal_indices(frame)
    entry_target_parts = []
    for idx in signals.values():
        if len(idx):
            base = frame.bin_ms.to_numpy(np.int64)[idx] + BIN_MS
            for latency in LATENCIES:
                entry_target_parts.append(base + latency)
    if not entry_target_parts:
        return {}
    entry_targets = np.unique(np.concatenate(entry_target_parts))
    entry_unique, entry_resolved = resolve_binance(binance_path, entry_targets, ENTRY_MAX_DELAY_MS)
    exit_target_parts = []
    cached_entries: dict[tuple[str,int,float,int],tuple[np.ndarray,np.ndarray,np.ndarray,np.ndarray]] = {}
    for key, idx in signals.items():
        if not len(idx):
            continue
        base = frame.bin_ms.to_numpy(np.int64)[idx] + BIN_MS
        side = signal_sides(frame, *key[:2], idx)
        for latency in LATENCIES:
            targets = base + latency
            actual, prices = lookup_resolved(entry_unique, entry_resolved, targets)
            good = (actual >= 0) & np.isfinite(prices) & (prices > 0)
            cached_entries[key + (latency,)] = (actual, prices, side, good)
            for horizon in HORIZONS:
                exit_target_parts.append(actual[good] + horizon)
    if not exit_target_parts:
        return {}
    exit_targets = np.unique(np.concatenate(exit_target_parts))
    exit_unique, exit_resolved = resolve_binance(binance_path, exit_targets, EXIT_MAX_DELAY_MS)
    output: dict[tuple[str,int,float,int,int],pd.DataFrame] = {}
    for key_latency, (actual, prices, side, entry_good) in cached_entries.items():
        for horizon in HORIZONS:
            exit_target = actual + horizon
            use = entry_good.copy()
            exit_time = np.full(len(actual), -1, dtype=np.int64)
            exit_price = np.full(len(actual), np.nan)
            if bool(use.any()):
                et, ep = lookup_resolved(exit_unique, exit_resolved, exit_target[use])
                exit_time[use] = et
                exit_price[use] = ep
            good = use & (exit_time >= 0) & np.isfinite(exit_price) & (exit_price > 0) & (exit_time > actual)
            candidate = key_latency + (horizon,)
            if not bool(good.any()):
                output[candidate] = pd.DataFrame(columns=['entry_ms','exit_ms','symbol','day','side','gross'])
                continue
            gross = side[good].astype(float) * (exit_price[good] / prices[good] - 1.0)
            output[candidate] = pd.DataFrame({'entry_ms':actual[good],'exit_ms':exit_time[good],'symbol':symbol,'day':day,'side':side[good],'gross':gross})
    return output


def route_day(parts: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [part for part in parts if not part.empty]
    if not valid:
        return pd.DataFrame(columns=['entry_ms','exit_ms','symbol','day','side','gross'])
    combined = pd.concat(valid, ignore_index=True).sort_values(['entry_ms','symbol'], kind='mergesort')
    rows = []
    free = -1
    for entry, group in combined.groupby('entry_ms', sort=True):
        entry = int(entry)
        if entry < free:
            continue
        row = group.iloc[0]
        rows.append(row.to_dict())
        free = int(row.exit_ms)
    return pd.DataFrame(rows)


def candidate_id(key: tuple[str,int,float,int,int]) -> str:
    rule, window, threshold, latency, horizon = key
    return f'{rule}|w{window}|z{threshold:g}|l{latency}|h{horizon}'


def build_day_command(args: argparse.Namespace) -> int:
    calendars = {'dev':DEV_DAYS,'val':VAL_DAYS,'final':FINAL_DAYS}
    if args.day not in calendars[args.phase]:
        raise ValueError('day is not in frozen calendar')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    day_start_ms = int(pd.Timestamp(args.day, tz='UTC').timestamp() * 1000)
    by_candidate: dict[tuple[str,int,float,int,int],list[pd.DataFrame]] = {}
    sources = []
    diagnostics = []
    for symbol in SYMBOLS:
        bpath, bmeta = ensure_binance(args.cache_dir, symbol, args.day)
        ypath, ymeta = ensure_bybit(args.cache_dir, symbol, args.day)
        binned = aggregate_100ms(_binance_chunks(bpath), day_start_ms)
        yinned = aggregate_100ms(_bybit_chunks(ypath), day_start_ms)
        frame = build_panel(binned, yinned, day_start_ms)
        legs = candidate_legs(frame, bpath, symbol, args.day)
        for key, ledger in legs.items():
            by_candidate.setdefault(key, []).append(ledger)
        sources.extend([{'symbol':symbol,'day':args.day,**bmeta},{'symbol':symbol,'day':args.day,**ymeta}])
        diagnostics.append({'symbol':symbol,'panel_rows':int(len(frame)),'binance_source_rows':int(binned['source_rows'][0]),'bybit_source_rows':int(yinned['source_rows'][0]),'candidate_keys':int(len(legs))})
        bpath.unlink(missing_ok=True)
        (args.cache_dir / f'{bpath.name}.CHECKSUM').unlink(missing_ok=True)
        ypath.unlink(missing_ok=True)
    rows = []
    expected_keys = [(rule,window,z,latency,horizon) for rule in RULES for window in WINDOWS for z in ZS for latency in LATENCIES for horizon in HORIZONS]
    for key in expected_keys:
        routed = route_day(by_candidate.get(key, []))
        if routed.empty:
            continue
        routed.insert(0, 'candidate_id', candidate_id(key))
        rows.append(routed)
    ledger = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=['candidate_id','entry_ms','exit_ms','symbol','day','side','gross'])
    ledger = ledger.sort_values(['candidate_id','entry_ms','symbol'], kind='mergesort')
    ledger_path = args.output_dir / f'crossvenue_{args.phase}_{args.day}.csv.gz'
    ledger.to_csv(ledger_path, index=False, compression={'method':'gzip','compresslevel':6,'mtime':0})
    manifest = {'version':'CROSSVENUE_TICK_V3_STREAM','phase':args.phase,'day':args.day,'candidate_count':len(expected_keys),'ledger_rows':int(len(ledger)),'ledger_sha256':sha256(ledger_path),'entry_max_delay_ms':ENTRY_MAX_DELAY_MS,'exit_max_delay_ms':EXIT_MAX_DELAY_MS,'sources':sources,'diagnostics':diagnostics,'candidate_pnl_observed_before_transport_amendment':False,'orders_submitted':False,'paper_or_live_started':False}
    (args.output_dir / f'manifest_{args.phase}_{args.day}.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:manifest[k] for k in ('phase','day','candidate_count','ledger_rows','ledger_sha256')},indent=2))
    return 0


def stats(gross: np.ndarray, cost_bps: float) -> dict[str,float|int]:
    if not len(gross):
        return {'n':0,'mean_bps':-999.0,'trim10_bps':-999.0,'pf':0.0,'log_growth':-999.0,'top10_conc':1.0,'after_top10_log_growth':-999.0}
    net = gross.astype(float) - cost_bps / 10_000.0
    positive = net[net > 0]
    negative = -net[net < 0]
    order = np.argsort(net)[::-1]
    keep = np.ones(len(net), dtype=bool)
    keep[order[:min(10,len(net))]] = False
    return {'n':int(len(net)),'mean_bps':float(net.mean()*1e4),'trim10_bps':float(net[keep].mean()*1e4) if bool(keep.any()) else -999.0,'pf':float(positive.sum()/negative.sum()) if negative.sum()>0 else (999.0 if positive.sum()>0 else 0.0),'log_growth':float(np.log1p(net).sum()) if np.all(net>-1) else -999.0,'top10_conc':float(np.sort(positive)[-10:].sum()/positive.sum()) if positive.sum()>0 else 1.0,'after_top10_log_growth':float(np.log1p(net[keep]).sum()) if bool(keep.any()) and np.all(net[keep]>-1) else -999.0}


def parse_candidate(value: str) -> tuple[str,int,float,int,int]:
    rule, w, z, l, h = value.split('|')
    return rule, int(w[1:]), float(z[1:]), int(l[1:]), int(h[1:])


def evaluate_command(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(args.input_dir.rglob('crossvenue_*.csv.gz'))
    dev_files = [p for p in files if '_dev_' in p.name]
    val_files = [p for p in files if '_val_' in p.name]
    if len(dev_files) != len(DEV_DAYS) or len(val_files) != len(VAL_DAYS):
        raise ValueError(f'expected {len(DEV_DAYS)} dev and {len(VAL_DAYS)} val files')
    frames = {'dev':pd.concat([pd.read_csv(p) for p in dev_files],ignore_index=True),'val':pd.concat([pd.read_csv(p) for p in val_files],ignore_index=True)}
    expected_ids = [candidate_id((rule,window,z,latency,horizon)) for rule in RULES for window in WINDOWS for z in ZS for latency in LATENCIES for horizon in HORIZONS]
    rows=[]
    for cid in expected_ids:
        rule,window,z,latency,horizon=parse_candidate(cid)
        rec={'candidate_id':cid,'rule':rule,'window_ms':window,'z':z,'latency_ms':latency,'horizon_ms':horizon}
        for phase in ('dev','val'):
            selected=frames[phase].loc[frames[phase].candidate_id==cid,'gross'].to_numpy(float)
            for cost in COSTS_BPS:
                rec.update({f'{phase}_{int(cost)}bp_{key}':value for key,value in stats(selected,cost).items()})
        rec['eligible']=(rec['dev_12bp_n']>=150 and rec['val_12bp_n']>=150 and rec['dev_12bp_trim10_bps']>0 and rec['val_12bp_trim10_bps']>0 and rec['dev_18bp_trim10_bps']>0 and rec['val_18bp_trim10_bps']>0 and rec['dev_12bp_pf']>=1.1 and rec['val_12bp_pf']>=1.1 and rec['dev_12bp_top10_conc']<=.35 and rec['val_12bp_top10_conc']<=.35 and rec['dev_12bp_after_top10_log_growth']>0 and rec['val_12bp_after_top10_log_growth']>0)
        rows.append(rec)
    screen=pd.DataFrame(rows).sort_values(['eligible','val_18bp_trim10_bps','candidate_id'],ascending=[False,False,True],kind='mergesort')
    screen.to_csv(args.output_dir/'screen_pre_final.csv',index=False)
    eligible=screen[screen.eligible]
    summary={'version':'CROSSVENUE_TICK_V3_STREAM','screened':int(len(screen)),'eligible_pre_final':int(len(eligible)),'final_opened':False,'final_open_permitted':bool(len(eligible)),'candidate_pnl_observed_before_transport_amendment':False,'orders_submitted':False,'paper_or_live_started':False,'validated':False,'best_pre_final':screen.head(20).replace([np.nan,np.inf,-np.inf],None).to_dict('records')}
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ('screened','eligible_pre_final','final_opened','final_open_permitted')},indent=2))
    return 0


def main() -> int:
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build-day')
    build.add_argument('--phase',choices=('dev','val','final'),required=True)
    build.add_argument('--day',required=True)
    build.add_argument('--cache-dir',type=Path,required=True)
    build.add_argument('--output-dir',type=Path,required=True)
    evaluate=sub.add_parser('evaluate')
    evaluate.add_argument('--input-dir',type=Path,required=True)
    evaluate.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    return build_day_command(args) if args.command=='build-day' else evaluate_command(args)


if __name__=='__main__':
    raise SystemExit(main())
