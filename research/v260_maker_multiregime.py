from __future__ import annotations
import argparse,csv,hashlib,io,json,math,time,urllib.error,urllib.request,zipfile
from pathlib import Path
import numpy as np,pandas as pd
BASE='https://data.binance.vision/data/futures/um/daily';UA='smc-v260-maker/1.0'

def fetch(url,attempts=5):
 err=None
 for i in range(attempts):
  try:
   req=urllib.request.Request(url,headers={'User-Agent':UA})
   with urllib.request.urlopen(req,timeout=300) as r:return r.read()
  except urllib.error.HTTPError as e:
   if e.code==404:raise
   err=e
  except Exception as e:err=e
  if i+1<attempts:time.sleep(min(2**i,20))
 raise RuntimeError(f'{url}: {err!r}')

def verified(url):
 chk=fetch(url+'.CHECKSUM').decode('utf-8-sig').strip().split()[0].lower();payload=fetch(url);actual=hashlib.sha256(payload).hexdigest()
 if actual!=chk:raise ValueError(f'checksum mismatch {url}')
 return payload,actual

def csvzip(payload):
 with zipfile.ZipFile(io.BytesIO(payload)) as z:
  names=[n for n in z.namelist() if n.lower().endswith('.csv')]
  if len(names)!=1:raise ValueError(names)
  raw=z.read(names[0]);first=raw.splitlines()[0].decode('utf-8-sig','replace').split(',');header=0 if any(any(c.isalpha() for c in x) for x in first) else None
  return pd.read_csv(io.BytesIO(raw),header=header,low_memory=False)

def dates(month):
 p=pd.Period(month,freq='M');days=pd.date_range(p.start_time,p.end_time,freq='D');order=[15,10,20,5,25,1,28,12,18,8,22]
 seen=set()
 for d in order:
  if d<=len(days):seen.add(d);yield days[d-1].date().isoformat()
 for x in days:
  if x.day not in seen:yield x.date().isoformat()

def locate(symbol,month):
 for day in dates(month):
  ok=True;urls={}
  for kind in ('bookTicker','aggTrades'):
   name=f'{symbol}-{kind}-{day}.zip';url=f'{BASE}/{kind}/{symbol}/{name}'
   try:fetch(url+'.CHECKSUM',attempts=1);urls[kind]=url
   except Exception:ok=False;break
  if ok:return day,urls
 raise RuntimeError(f'no paired day {symbol} {month}')

def parse_book(payload):
 d=csvzip(payload)
 if d.shape[1]<7:raise ValueError('book width')
 d=d.iloc[:,:7];d.columns=['update_id','bid','bid_qty','ask','ask_qty','transaction_time','event_time']
 for c in d.columns:d[c]=pd.to_numeric(d[c],errors='coerce')
 t=d.transaction_time.astype('Int64');m=float(t.dropna().abs().median());unit='us' if m>1e14 else 'ms';d['time']=pd.to_datetime(t,unit=unit,utc=True,errors='coerce');return d.dropna().sort_values('time')

def parse_trades(payload):
 d=csvzip(payload)
 if d.shape[1]<7:raise ValueError('trade width')
 d=d.iloc[:,:7];d.columns=['agg_id','price','qty','first_id','last_id','time_raw','buyer_maker']
 for c in ('price','qty','time_raw'):d[c]=pd.to_numeric(d[c],errors='coerce')
 t=d.time_raw.astype('Int64');m=float(t.dropna().abs().median());unit='us' if m>1e14 else 'ms';d['time']=pd.to_datetime(t,unit=unit,utc=True,errors='coerce');d['buyer_maker']=d.buyer_maker.astype(str).str.lower().isin(('true','1'));d['quote']=d.price*d.qty;d['signed_quote']=np.where(d.buyer_maker,-d.quote,d.quote);return d.dropna(subset=['time','price','qty']).sort_values('time')

def zprior(s,w):return (s-s.rolling(w,min_periods=max(30,w//3)).mean().shift(1))/s.rolling(w,min_periods=max(30,w//3)).std(ddof=0).shift(1).replace(0,np.nan)

def build_day(symbol,month,out):
 day,urls=locate(symbol,month);bp,bsha=verified(urls['bookTicker']);tp,tsha=verified(urls['aggTrades']);book=parse_book(bp);tr=parse_trades(tp)
 start=pd.Timestamp(day,tz='UTC');idx=pd.date_range(start,start+pd.Timedelta(days=1),freq='1s',inclusive='left');b=book.set_index('time')[['bid','bid_qty','ask','ask_qty']].resample('1s').last().reindex(idx).ffill();b['mid']=(b.bid+b.ask)/2;b['spread_bps']=(b.ask-b.bid)/b.mid*1e4;b['imb']=(b.bid_qty-b.ask_qty)/(b.bid_qty+b.ask_qty);b['micro']=(b.ask*b.bid_qty+b.bid*b.ask_qty)/(b.bid_qty+b.ask_qty);b['micro_bps']=(b.micro-b.mid)/b.mid*1e4
 sec=tr.time.dt.floor('s');g=pd.DataFrame({'second':sec,'buy_quote':np.where(~tr.buyer_maker,tr.quote,0),'sell_quote':np.where(tr.buyer_maker,tr.quote,0),'signed':tr.signed_quote}).groupby('second').sum().reindex(idx,fill_value=0);b=b.join(g);b['flow1']=b.signed/(b.buy_quote+b.sell_quote).replace(0,np.nan);b['flow5']=b.signed.rolling(5).sum()/(b.buy_quote.add(b.sell_quote).rolling(5).sum()).replace(0,np.nan);b['flow15']=b.signed.rolling(15).sum()/(b.buy_quote.add(b.sell_quote).rolling(15).sum()).replace(0,np.nan);b['ret1_bps']=np.log(b.mid/b.mid.shift(1))*1e4;b['ret5_bps']=np.log(b.mid/b.mid.shift(5))*1e4;b['imb_z']=zprior(b.imb,300);b['flow5_z']=zprior(b.flow5,300);b['micro_z']=zprior(b.micro_bps,300)
 # exact raw trades indexed for queue consumption and markout; one row per second candidate after warmup
 times=tr.time.view('i8').to_numpy();prices=tr.price.to_numpy(float);qty=tr.qty.to_numpy(float);maker=tr.buyer_maker.to_numpy(bool)
 mids=b.mid.to_numpy(float);records=[]
 for i in range(300,len(b)-65):
  if not np.isfinite(mids[i]) or b.spread_bps.iloc[i]>5:continue
  order_sec=idx[i+1];start_ns=order_sec.value;end_ns=(order_sec+pd.Timedelta(seconds=5)).value;lo=np.searchsorted(times,start_ns,'left');hi=np.searchsorted(times,end_ns,'left')
  for side in (1,-1):
   limit=float(b.bid.iloc[i+1] if side>0 else b.ask.iloc[i+1]);display=float(b.bid_qty.iloc[i+1] if side>0 else b.ask_qty.iloc[i+1]);sel=(maker[lo:hi]&(prices[lo:hi]<=limit)) if side>0 else ((~maker[lo:hi])&(prices[lo:hi]>=limit));cum=np.cumsum(qty[lo:hi][sel]) if np.any(sel) else np.array([])
   trade_times=times[lo:hi][sel] if np.any(sel) else np.array([],dtype=np.int64)
   for qf in (1.,1.5,2.):
    need=display*qf;filled=len(cum)>0 and cum[-1]>=need
    fill_ns=int(trade_times[np.searchsorted(cum,need,'left')]) if filled else -1
    fill_sec=pd.Timestamp(fill_ns,tz='UTC').floor('s') if filled else pd.NaT
    for h in (5,10,30,60):
     if filled:
      ex=fill_sec+pd.Timedelta(seconds=h);j=idx.searchsorted(ex);j=min(j,len(idx)-1);gross=side*(mids[j]/limit-1);path=mids[idx.searchsorted(fill_sec):j+1];mae=float(np.min(side*(path/limit-1))) if len(path) else 0.;exit_time=idx[j]
     else:gross=mae=np.nan;exit_time=pd.NaT
     records.append({'signal_time':idx[i],'entry_time':fill_sec,'exit_time':exit_time,'symbol':symbol,'month':month,'side':side,'queue_factor':qf,'horizon_s':h,'filled':filled,'gross':gross,'mae':mae,'imb':b.imb.iloc[i],'imb_z':b.imb_z.iloc[i],'flow1':b.flow1.iloc[i],'flow5':b.flow5.iloc[i],'flow15':b.flow15.iloc[i],'flow5_z':b.flow5_z.iloc[i],'micro_bps':b.micro_bps.iloc[i],'micro_z':b.micro_z.iloc[i],'spread_bps':b.spread_bps.iloc[i],'ret1_bps':b.ret1_bps.iloc[i],'ret5_bps':b.ret5_bps.iloc[i]})
 out=Path(out);out.mkdir(parents=True,exist_ok=True);q=pd.DataFrame(records);path=out/f'{symbol}_{month}.csv.gz';q.to_csv(path,index=False,compression={'method':'gzip','compresslevel':6,'mtime':0});(out/f'{symbol}_{month}.json').write_text(json.dumps({'symbol':symbol,'month':month,'day':day,'rows':len(q),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'book':{'url':urls['bookTicker'],'sha256':bsha,'raw_rows':len(book)},'trades':{'url':urls['aggTrades'],'sha256':tsha,'raw_rows':len(tr)}},indent=2))

def metric(rets,months):
 if len(rets)==0:return {'trades':0,'net':0.,'gday':0.,'mdd':0.,'pf':0.}
 e=np.cumprod(1+np.maximum(rets,-.999));curve=np.r_[1.,e];dd=np.max(1-curve/np.maximum.accumulate(curve));pos=rets[rets>0].sum();neg=-rets[rets<0].sum();days=max(1,months*30);return {'trades':len(rets),'net':float(e[-1]-1),'gday':float(np.exp(np.log(max(e[-1],1e-12))/days)-1),'mdd':float(dd),'pf':float(pos/neg) if neg>0 else 999.}

def aggregate(inp,out):
 files=sorted(Path(inp).rglob('*.csv.gz'));d=pd.concat([pd.read_csv(p) for p in files],ignore_index=True);d.entry_time=pd.to_datetime(d.entry_time,utc=True,errors='coerce');d.exit_time=pd.to_datetime(d.exit_time,utc=True,errors='coerce');d=d[d.filled].dropna(subset=['entry_time','gross']).sort_values(['entry_time','symbol'])
 rows=[]
 for family in ('continuation','flow','micro_reversion','micro_continuation'):
  for th in (.6,.7,.8,.9):
   for z in (0.,1.,2.,3.):
    if family=='continuation':mask=(np.sign(d.imb)==d.side)&(d.imb.abs()>=th)&(np.sign(d.flow5)==d.side)&(d.flow5_z.abs()>=z)&(np.sign(d.micro_bps)==d.side);score=d.imb.abs()+d.flow5_z.abs()+d.micro_z.abs()
    elif family=='flow':mask=(np.sign(d.flow5)==d.side)&(d.flow5.abs()>=th)&(d.flow5_z.abs()>=z);score=d.flow5.abs()+d.flow5_z.abs()
    elif family=='micro_reversion':mask=(np.sign(d.micro_bps)==-d.side)&(d.micro_z.abs()>=z)&(np.sign(d.flow1)==d.side);score=d.micro_z.abs()+d.flow1.abs()
    else:mask=(np.sign(d.micro_bps)==d.side)&(d.micro_z.abs()>=z)&(np.sign(d.flow5)==d.side);score=d.micro_z.abs()+d.flow5_z.abs()
    q=d[mask].copy();q['score']=score[mask]
    for qf in (1.,1.5,2.):
     for h in (5,10,30,60):
      x=q[(q.queue_factor==qf)&(q.horizon_s==h)].sort_values(['entry_time','score','symbol'],ascending=[True,False,True],kind='mergesort')
      for stop in (10.,20.,30.,50.):
       y=x[x.mae>=-stop/1e4].copy();y['gross_stop']=np.where(x.loc[y.index,'mae']<=-stop/1e4,-stop/1e4,y.gross)
       for cost in (8.,12.,18.):
        free=pd.Timestamp.min.tz_localize('UTC');rr=[];tt=[]
        for _,r in y.iterrows():
         if r.entry_time<free:continue
         rr.append(float(r.gross_stop-cost/1e4));tt.append(r.entry_time);free=r.exit_time
        t=pd.DataFrame({'entry':tt,'ret':rr});row={'family':family,'threshold':th,'z':z,'queue_factor':qf,'horizon_s':h,'stop_bps':stop,'cost_bps':cost}
        for n,a,b,mons in [('DEV','2023-05-01','2023-11-01',6),('VALID','2023-11-01','2024-04-01',5)]:
         u=t[(t.entry>=a)&(t.entry<b)];m=metric(u.ret.to_numpy(float),mons);row.update({f'{n}_{k}':v for k,v in m.items()})
        rows.append(row)
 r=pd.DataFrame(rows);r['min_gday']=r[['DEV_gday','VALID_gday']].min(axis=1);r['max_mdd']=r[['DEV_mdd','VALID_mdd']].max(axis=1);r['min_trades']=r[['DEV_trades','VALID_trades']].min(axis=1);r=r.sort_values(['min_gday','max_mdd'],ascending=[False,True]);out=Path(out);out.mkdir(parents=True,exist_ok=True);r.to_csv(out/'rank.csv',index=False);r.to_csv(out/'screen.csv',index=False);e=r[(r.min_gday>0)&(r.max_mdd<=.3)&(r.min_trades>=100)];e.to_csv(out/'eligible.csv',index=False);tg=r[(r.min_gday>=.01)&(r.max_mdd<=.3)&(r.min_trades>=300)];tg.to_csv(out/'target_met.csv',index=False);(out/'summary.json').write_text(json.dumps({'status':'COMPLETE','files':len(files),'rows':len(r),'eligible':len(e),'target_met':len(tg),'best':r.head(30).replace([np.nan,np.inf,-np.inf],None).to_dict('records')},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();sp=p.add_subparsers(dest='cmd',required=True);a=sp.add_parser('day');a.add_argument('--symbol',required=True);a.add_argument('--month',required=True);a.add_argument('--out',required=True);b=sp.add_parser('aggregate');b.add_argument('--input',required=True);b.add_argument('--out',required=True);z=p.parse_args();build_day(z.symbol,z.month,z.out) if z.cmd=='day' else aggregate(z.input,z.out)
