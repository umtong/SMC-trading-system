from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = 'https://data.binance.vision/data/futures/um/monthly'
AGG_COLUMNS = (
    'agg_trade_id', 'price', 'quantity', 'first_trade_id', 'last_trade_id',
    'transact_time', 'is_buyer_maker',
)
KLINE_COLUMNS = (
    'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
    'quote_volume', 'trade_count', 'taker_buy_base', 'taker_buy_quote', 'ignore',
)


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument('--symbol', required=True)
    p.add_argument('--month', required=True, help='YYYY-MM')
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--chunk-rows', type=int, default=750_000)
    p.add_argument('--retries', type=int, default=5)
    return p.parse_args()


def fetch(url: str, retries: int) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'wave44-quarterhour-research/1.0'})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 == retries:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f'download failed: {url}') from last


def checksum_text(payload: bytes) -> str:
    token = payload.decode('utf-8-sig').strip().split()[0].lower()
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError(f'invalid checksum payload {token!r}')
    return token


def download_verified(url: str, retries: int) -> tuple[bytes, dict[str, object]]:
    expected = checksum_text(fetch(url + '.CHECKSUM', retries))
    payload = fetch(url, retries)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f'checksum mismatch {url}: {actual} != {expected}')
    return payload, {'url': url, 'sha256': actual, 'bytes': len(payload)}


def csv_member(payload: bytes) -> tuple[zipfile.ZipFile, str]:
    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = sorted(n for n in zf.namelist() if n.lower().endswith('.csv'))
    if len(names) != 1:
        zf.close()
        raise ValueError(f'expected one CSV, found {names}')
    return zf, names[0]


def bool_buyer_maker(s: pd.Series) -> pd.Series:
    text = s.astype(str).str.strip().str.lower()
    if not text.isin(['true', 'false', '1', '0']).all():
        bad = text[~text.isin(['true', 'false', '1', '0'])].head().tolist()
        raise ValueError(f'invalid buyer-maker values: {bad}')
    return text.isin(['true', '1'])


def normalize_agg_chunk(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.iloc[:, :7].copy()
    raw.columns = AGG_COLUMNS
    numeric_id = pd.to_numeric(raw['agg_trade_id'], errors='coerce')
    if numeric_id.isna().any():
        raw = raw[numeric_id.notna()].copy()
    if raw.empty:
        return pd.DataFrame(columns=['trade_time','trade_id','price','quantity','quote','buy_aggressor'])
    trade_id = pd.to_numeric(raw['agg_trade_id'], errors='raise').astype('int64')
    price = pd.to_numeric(raw['price'], errors='raise').astype(float)
    qty = pd.to_numeric(raw['quantity'], errors='raise').astype(float)
    ts = pd.to_numeric(raw['transact_time'], errors='raise').astype('int64')
    unit = 'ms' if int(ts.max()) < 10**14 else 'us'
    out = pd.DataFrame({
        'trade_time': pd.to_datetime(ts, unit=unit, utc=True),
        'trade_id': trade_id,
        'price': price,
        'quantity': qty,
        'quote': price * qty,
        'buy_aggressor': ~bool_buyer_maker(raw['is_buyer_maker']).to_numpy(),
    })
    if bool((out[['price','quantity','quote']] <= 0).any().any()):
        raise ValueError('non-positive trade fields')
    return out


def filtered_trade_windows(payload: bytes, chunk_rows: int) -> tuple[pd.DataFrame, int, int]:
    zf, member = csv_member(payload)
    selected: list[pd.DataFrame] = []
    total_rows = 0
    try:
        with zf.open(member) as handle:
            for raw in pd.read_csv(handle, header=None, chunksize=chunk_rows, low_memory=False):
                d = normalize_agg_chunk(raw)
                total_rows += len(d)
                if d.empty:
                    continue
                floor = d['trade_time'].dt.floor('15min')
                post_offset_ms = (d['trade_time'] - floor).dt.total_seconds() * 1000.0
                ceil = d['trade_time'].dt.ceil('15min')
                pre_lead_ms = (ceil - d['trade_time']).dt.total_seconds() * 1000.0
                keep_post = (post_offset_ms >= 0) & (post_offset_ms < 60_000)
                keep_pre = (pre_lead_ms > 0) & (pre_lead_ms <= 60_000)
                k = keep_post | keep_pre
                if not bool(k.any()):
                    continue
                x = d.loc[k].copy()
                x['post_boundary'] = floor.loc[k]
                x['post_offset_ms'] = post_offset_ms.loc[k].to_numpy()
                x['pre_boundary'] = ceil.loc[k]
                x['pre_lead_ms'] = pre_lead_ms.loc[k].to_numpy()
                x['keep_post'] = keep_post.loc[k].to_numpy()
                x['keep_pre'] = keep_pre.loc[k].to_numpy()
                selected.append(x)
    finally:
        zf.close()
    if not selected:
        raise RuntimeError('no quarter-hour window trades found')
    out = pd.concat(selected, ignore_index=True)
    out.sort_values(['trade_time','trade_id'], inplace=True)
    return out, total_rows, len(out)


def summarize_group(g: pd.DataFrame, prefix: str) -> dict[str, float | int]:
    g = g.sort_values(['trade_time','trade_id'], kind='mergesort')
    quote = g['quote'].to_numpy(float)
    buy = g['buy_aggressor'].to_numpy(bool)
    price = g['price'].to_numpy(float)
    total = float(quote.sum())
    buy_q = float(quote[buy].sum())
    sell_q = total - buy_q
    count = len(g)
    buy_n = int(buy.sum())
    span = float(price.max() - price.min())
    logret = float(np.log(price[-1] / price[0]))
    top = np.sort(quote)[::-1]
    return {
        f'{prefix}_quote': total,
        f'{prefix}_buy_quote': buy_q,
        f'{prefix}_sell_quote': sell_q,
        f'{prefix}_signed_quote': buy_q - sell_q,
        f'{prefix}_imbalance': (buy_q - sell_q) / total if total > 0 else np.nan,
        f'{prefix}_agg_count': int(count),
        f'{prefix}_buy_agg_count': buy_n,
        f'{prefix}_sell_agg_count': int(count - buy_n),
        f'{prefix}_count_imbalance': (2 * buy_n - count) / count if count else np.nan,
        f'{prefix}_open': float(price[0]),
        f'{prefix}_high': float(price.max()),
        f'{prefix}_low': float(price.min()),
        f'{prefix}_close': float(price[-1]),
        f'{prefix}_vwap': float(np.dot(price, g['quantity'].to_numpy(float)) / g['quantity'].sum()),
        f'{prefix}_logret': logret,
        f'{prefix}_range_frac': span / price[0],
        f'{prefix}_price_efficiency': abs(price[-1] - price[0]) / span if span > 0 else 0.0,
        f'{prefix}_max_trade_quote': float(top[0]),
        f'{prefix}_top5_share': float(top[:5].sum() / total) if total > 0 else np.nan,
        f'{prefix}_first_trade_ms': int(g['trade_time'].iloc[0].value // 1_000_000),
        f'{prefix}_last_trade_ms': int(g['trade_time'].iloc[-1].value // 1_000_000),
    }


def aggregate_windows(trades: pd.DataFrame, month: str) -> pd.DataFrame:
    month_start = pd.Timestamp(month + '-01', tz='UTC')
    month_end = month_start + pd.offsets.MonthBegin(1)
    boundaries = pd.date_range(month_start, month_end, freq='15min', inclusive='left')
    rows: dict[pd.Timestamp, dict[str, object]] = {b: {'boundary': b} for b in boundaries}

    post = trades[trades['keep_post']].copy()
    post = post[(post['post_boundary'] >= month_start) & (post['post_boundary'] < month_end)]
    for window_s in (10, 30, 60):
        x = post[post['post_offset_ms'] < window_s * 1000]
        prefix = f'post{window_s}'
        for b, g in x.groupby('post_boundary', sort=True):
            rows[b].update(summarize_group(g, prefix))
            rows[b][f'{prefix}_first_delay_ms'] = int(g['post_offset_ms'].min())

    pre = trades[trades['keep_pre']].copy()
    pre = pre[(pre['pre_boundary'] > month_start) & (pre['pre_boundary'] < month_end)]
    for window_s in (10, 30, 60):
        x = pre[pre['pre_lead_ms'] <= window_s * 1000]
        prefix = f'pre{window_s}'
        for b, g in x.groupby('pre_boundary', sort=True):
            rows[b].update(summarize_group(g, prefix))

    out = pd.DataFrame(rows.values()).sort_values('boundary')
    required = ['post10_quote','post30_quote','post60_quote','pre10_quote','pre30_quote','pre60_quote']
    out = out[out['boundary'] > month_start].copy()
    missing = out[required].isna().any(axis=1)
    if bool(missing.any()):
        bad = out.loc[missing, 'boundary'].head().astype(str).tolist()
        raise RuntimeError(f'incomplete exact trade windows: count={int(missing.sum())} first={bad}')
    return out


def parse_klines(payload: bytes) -> pd.DataFrame:
    zf, member = csv_member(payload)
    try:
        with zf.open(member) as handle:
            raw = pd.read_csv(handle, header=None, low_memory=False)
    finally:
        zf.close()
    raw = raw.iloc[:, :12].copy(); raw.columns = KLINE_COLUMNS
    ot = pd.to_numeric(raw['open_time'], errors='coerce')
    raw = raw[ot.notna()].copy(); ot = pd.to_numeric(raw['open_time'], errors='raise').astype('int64')
    unit = 'ms' if int(ot.max()) < 10**14 else 'us'
    raw['boundary'] = pd.to_datetime(ot, unit=unit, utc=True)
    for c in ['open','high','low','close','quote_volume','taker_buy_quote']:
        raw[c] = pd.to_numeric(raw[c], errors='raise').astype(float)
    return raw[['boundary','open','high','low','close','quote_volume','taker_buy_quote']]


def validate_against_klines(features: pd.DataFrame, klines: pd.DataFrame) -> dict[str, float | int]:
    q = features.merge(klines, on='boundary', how='left', validate='one_to_one')
    if q[['open','high','low','close','quote_volume','taker_buy_quote']].isna().any().any():
        raise RuntimeError('missing official 1m kline validation rows')
    scale = q['quote_volume'].abs().clip(lower=1.0)
    buy_scale = q['taker_buy_quote'].abs().clip(lower=1.0)
    rel_quote = ((q['post60_quote'] - q['quote_volume']).abs() / scale)
    rel_buy = ((q['post60_buy_quote'] - q['taker_buy_quote']).abs() / buy_scale)
    price_err = np.max(np.abs(q[['post60_open','post60_high','post60_low','post60_close']].to_numpy() - q[['open','high','low','close']].to_numpy()))
    stats = {
        'rows': int(len(q)),
        'max_relative_quote_error': float(rel_quote.max()),
        'max_relative_taker_buy_quote_error': float(rel_buy.max()),
        'max_absolute_ohlc_error': float(price_err),
    }
    if stats['max_relative_quote_error'] > 2e-6 or stats['max_relative_taker_buy_quote_error'] > 2e-6 or stats['max_absolute_ohlc_error'] > 1e-8:
        raise RuntimeError(f'aggTrade-to-kline validation failed: {stats}')
    return stats


def main() -> int:
    a = args(); symbol = a.symbol.upper(); month = a.month
    outdir = a.output_dir; outdir.mkdir(parents=True, exist_ok=True)
    agg_url = f'{ROOT}/aggTrades/{symbol}/{symbol}-aggTrades-{month}.zip'
    kline_url = f'{ROOT}/klines/{symbol}/1m/{symbol}-1m-{month}.zip'
    print(f'download {agg_url}', flush=True)
    agg_payload, agg_meta = download_verified(agg_url, a.retries)
    print(f'download {kline_url}', flush=True)
    kline_payload, kline_meta = download_verified(kline_url, a.retries)
    trades, total_rows, selected_rows = filtered_trade_windows(agg_payload, a.chunk_rows)
    features = aggregate_windows(trades, month)
    klines = parse_klines(kline_payload)
    validation = validate_against_klines(features, klines)
    features.insert(0, 'symbol', symbol)
    path = outdir / f'{symbol}_quarterhour_exact_{month}.parquet'
    features.to_parquet(path, index=False, compression='zstd')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        'schema': 'wave44-quarterhour-exact-v1',
        'symbol': symbol,
        'month': month,
        'decision_clock': 'all post10 features are available only after boundary+10 seconds',
        'monthly_first_boundary_removed': True,
        'aggregate_trade_rows': int(total_rows),
        'selected_window_rows': int(selected_rows),
        'feature_rows': int(len(features)),
        'source': {'aggTrades': agg_meta, 'klines_1m': kline_meta},
        'validation': validation,
        'output': {'path': path.name, 'bytes': path.stat().st_size, 'sha256': digest},
        'holdout_protection': '2022 development features only; no 2023+ files requested',
    }
    (outdir / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2), flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
