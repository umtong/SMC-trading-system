from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

RULES = [
    {'id':'R1_abs10_qz2_opp','w':10,'flow_abs':0.5,'qz':2.0,'impact_max':2.0,'book':'opp','horizon':1800},
    {'id':'R2_abs30_qz1_plain','w':30,'flow_abs':0.3,'qz':1.0,'impact_max':2.0,'book':'none','horizon':1800},
    {'id':'R3_abs10_qz3_plain','w':10,'flow_abs':0.3,'qz':3.0,'impact_max':2.0,'book':'none','horizon':1800},
    {'id':'R4_abs5_qz2_replenish','w':5,'flow_abs':0.5,'qz':2.0,'impact_max':1.0,'book':'replenish','horizon':1800},
    {'id':'R5_abs10_qz1_zeroimpact','w':10,'flow_abs':0.5,'qz':1.0,'impact_max':0.0,'book':'none','horizon':1800},
]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_second_panel(symbol: str, day: str, root: Path) -> pd.DataFrame:
    book_path = root / f'{symbol}-bookTicker-{day}.zip'
    trade_path = root / f'{symbol}-aggTrades-{day}.zip'
    book = pd.read_csv(
        book_path, compression='zip',
        usecols=['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','transaction_time'],
        dtype={'best_bid_price':'float64','best_bid_qty':'float64','best_ask_price':'float64','best_ask_qty':'float64','transaction_time':'int64'},
    ).sort_values('transaction_time', kind='mergesort')
    book['sec'] = book.transaction_time // 1000
    b = book.groupby('sec', sort=False).agg(
        bid=('best_bid_price','last'), ask=('best_ask_price','last'),
        bid_qty=('best_bid_qty','last'), ask_qty=('best_ask_qty','last'),
        book_updates=('transaction_time','size'), last_book_ms=('transaction_time','last'),
    ).reset_index()
    del book
    b['mid'] = (b.bid + b.ask) / 2.0
    den = b.bid_qty + b.ask_qty
    b['book_imbalance'] = np.where(den > 0, (b.bid_qty - b.ask_qty) / den, 0.0)
    b['microprice'] = np.where(den > 0, (b.ask*b.bid_qty + b.bid*b.ask_qty)/den, b.mid)
    b['microprice_bps'] = (b.microprice / b.mid - 1.0) * 1e4

    t = pd.read_csv(
        trade_path, compression='zip',
        usecols=['price','quantity','transact_time','is_buyer_maker'],
        dtype={'price':'float64','quantity':'float64','transact_time':'int64','is_buyer_maker':'bool'},
    ).sort_values('transact_time', kind='mergesort')
    t['sec'] = t.transact_time // 1000
    t['quote'] = t.price * t.quantity
    t['signed'] = np.where(t.is_buyer_maker, -t.quote, t.quote)
    g = t.groupby('sec', sort=False).agg(
        trade_open=('price','first'), trade_close=('price','last'),
        quote_volume=('quote','sum'), signed_quote=('signed','sum'),
        agg_rows=('transact_time','size'), first_trade_ms=('transact_time','first'),
    ).reset_index()
    del t
    lo = max(int(b.sec.min()), int(g.sec.min()))
    hi = min(int(b.sec.max()), int(g.sec.max()))
    x = pd.DataFrame({'sec':np.arange(lo, hi+1, dtype=np.int64)})
    x = x.merge(b,on='sec',how='left',validate='one_to_one').merge(g,on='sec',how='left',validate='one_to_one')
    for c in ['bid','ask','bid_qty','ask_qty','mid','book_imbalance','microprice','microprice_bps','last_book_ms']:
        x[c] = x[c].ffill()
    for c in ['quote_volume','signed_quote','agg_rows']:
        x[c] = x[c].fillna(0.0)
    x['state_price'] = x.trade_close.fillna(x.mid)
    x['entry_price'] = x.trade_open.bfill(limit=2).shift(-1)
    x['entry_sec'] = x.sec.where(x.trade_open.notna()).bfill(limit=2).shift(-1)
    for w in sorted({r['w'] for r in RULES}):
        q = x.quote_volume.rolling(w,min_periods=w).sum()
        s = x.signed_quote.rolling(w,min_periods=w).sum()
        x[f'flow_{w}'] = np.where(q>0,s/q,0.0)
        x[f'ret_{w}'] = np.log(x.state_price/x.state_price.shift(w))*1e4
        logq = np.log1p(q)
        mu = logq.shift(1).rolling(3600,min_periods=1200).mean()
        sd = logq.shift(1).rolling(3600,min_periods=1200).std(ddof=0)
        x[f'qz_{w}'] = (logq-mu)/sd.replace(0,np.nan)
        x[f'bi_delta_{w}'] = x.book_imbalance-x.book_imbalance.shift(w)
        x[f'ask_delta_{w}'] = x.ask_qty-x.ask_qty.shift(w)
        x[f'bid_delta_{w}'] = x.bid_qty-x.bid_qty.shift(w)
    for h in sorted({r['horizon'] for r in RULES}):
        shifted = x.trade_open.shift(-h)
        x[f'exit_price_{h}'] = shifted.bfill(limit=2)
        x[f'exit_sec_{h}'] = x.sec.shift(-h).where(shifted.notna()).bfill(limit=2)
    x['time'] = pd.to_datetime(x.sec, unit='s', utc=True)
    x['symbol'] = symbol
    return x


def route(q: pd.DataFrame) -> pd.DataFrame:
    if q.empty:
        return q
    q = q.sort_values(['decision_sec','score'],ascending=[True,False],kind='mergesort').reset_index(drop=True)
    keep=[];free=-10**18
    for i,r in enumerate(q.itertuples(index=False)):
        if r.decision_sec < free:
            continue
        keep.append(i)
        free = int(r.exit_sec)+1
    return q.iloc[keep].copy().reset_index(drop=True)


def analyze_rule(x: pd.DataFrame, rule: dict, day: str) -> tuple[pd.DataFrame,dict]:
    w=rule['w'];fs=np.sign(x[f'flow_{w}']).astype(np.int8);side=-fs
    impact=fs*x[f'ret_{w}']
    mask=(x[f'flow_{w}'].abs()>=rule['flow_abs'])&(x[f'qz_{w}']>=rule['qz'])&(impact<=rule['impact_max'])&(side!=0)
    if rule['book']=='opp':
        mask &= (fs*x.book_imbalance<=-.15)&(fs*x[f'bi_delta_{w}']<0)
    elif rule['book']=='replenish':
        replen=np.where(fs>0,x[f'ask_delta_{w}'],x[f'bid_delta_{w}'])
        mask &= (replen>0)&(fs*x[f'bi_delta_{w}']<0)
    h=rule['horizon']
    cols=['time','sec','symbol','entry_price','entry_sec',f'exit_price_{h}',f'exit_sec_{h}',f'flow_{w}',f'qz_{w}',f'ret_{w}','book_imbalance',f'bi_delta_{w}']
    q=x.loc[mask,cols].copy().rename(columns={'time':'decision_time','sec':'decision_sec',f'exit_price_{h}':'exit_price',f'exit_sec_{h}':'exit_sec',f'flow_{w}':'flow',f'qz_{w}':'qz',f'ret_{w}':'window_return_bps',f'bi_delta_{w}':'book_imbalance_delta'})
    q['side']=side[mask].to_numpy(np.int8)
    q['score']=q.qz+q.flow.abs()+.2*np.maximum(rule['impact_max']-np.sign(q.flow)*q.window_return_bps,0)
    q['rule_id']=rule['id'];q['day']=day;q['horizon_seconds']=h
    q=q[np.isfinite(q.entry_price)&np.isfinite(q.exit_price)&(q.entry_sec<=q.decision_sec+3)&(q.exit_sec>=q.entry_sec)].copy()
    q['gross_bps']=q.side*np.log(q.exit_price/q.entry_price)*1e4
    q=route(q)
    rec={'symbol':str(x.symbol.iloc[0]),'day':day,'rule_id':rule['id'],'raw_candidates':int(mask.sum()),'trades':len(q)}
    for cost in (6.0,10.0,14.0):
        net=q.gross_bps-cost;pos=net[net>0].sum();neg=-net[net<0].sum()
        rec[f'net_{int(cost)}bps']=float(net.sum());rec[f'mean_{int(cost)}bps']=float(net.mean()) if len(net) else None;rec[f'pf_{int(cost)}bps']=float(pos/neg) if neg else (999.0 if len(net) else 0.0);rec[f'win_{int(cost)}bps']=float((net>0).mean()) if len(net) else 0.0
    return q,rec


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--symbol',required=True);ap.add_argument('--root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    manifest=json.loads((a.root/'MANIFEST.json').read_text());days=list(manifest['days']);a.output.mkdir(parents=True,exist_ok=True)
    ledgers=[];summ=[]
    for day in days:
        print('depletion-alpha',a.symbol,day,flush=True)
        x=load_second_panel(a.symbol,day,a.root)
        for rule in RULES:
            q,r=analyze_rule(x,rule,day);ledgers.append(q);summ.append(r)
    ledger=pd.concat(ledgers,ignore_index=True,sort=False) if ledgers else pd.DataFrame()
    summary=pd.DataFrame(summ)
    ledger.to_csv(a.output/f'{a.symbol}_DEPLETION_ALPHA_LEDGER.csv',index=False)
    summary.to_csv(a.output/f'{a.symbol}_DEPLETION_ALPHA_DAILY.csv',index=False)
    validation=summary[summary.day.ne('2023-05-16')].groupby('rule_id').agg(days=('day','nunique'),trades=('trades','sum'),net_6bps=('net_6bps','sum'),net_10bps=('net_10bps','sum'),net_14bps=('net_14bps','sum'),positive_days_10bps=('net_10bps',lambda s:int((s>0).sum())),worst_day_10bps=('net_10bps','min')).reset_index()
    validation.to_csv(a.output/f'{a.symbol}_DEPLETION_ALPHA_VALIDATION.csv',index=False)
    verdict={'schema_version':1,'symbol':a.symbol,'development_day':'2023-05-16','validation_days':[d for d in days if d!='2023-05-16'],'rules':RULES,'validation_survivors':validation[(validation.days==3)&(validation.trades>=6)&(validation.net_10bps>0)&(validation.net_14bps>0)&(validation.positive_days_10bps>=2)&(validation.worst_day_10bps>-25)].replace({np.nan:None}).to_dict('records'),'input_manifest_sha256':digest(a.root/'MANIFEST.json'),'paper_or_live_authority':False}
    (a.output/f'{a.symbol}_DEPLETION_ALPHA_VERDICT.json').write_text(json.dumps(verdict,indent=2)+'\n')
    print(json.dumps(verdict,indent=2)[:12000])

if __name__=='__main__':main()
