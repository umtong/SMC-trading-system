#!/usr/bin/env python3
from __future__ import annotations
import argparse,base64,bisect,csv,gzip,hashlib,io,json,shutil,urllib.request,zipfile
from pathlib import Path
BASE='https://data.binance.vision/data/futures/um/monthly/aggTrades'
COLS=('agg_trade_id','price','quantity','first_trade_id','last_trade_id','transact_time_ms','is_buyer_maker')
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
def get(url,p):
 q=urllib.request.Request(url,headers={'User-Agent':'smc-liquidation-v2/1.0'})
 with urllib.request.urlopen(q,timeout=600) as r,Path(p).open('wb') as o:shutil.copyfileobj(r,o,1<<20)
def checksum(p,name):
 s=Path(p).read_text().strip();xs=[x.lower() for x in s.replace('*',' ').split() if len(x)==64]
 if name not in s or not xs:raise ValueError('bad checksum')
 return xs[0]
def windows(part,symbol,month):
 enc=''.join(Path(part).read_text().split());raw=gzip.decompress(base64.b64decode(enc)).decode();rows=[]
 for r in csv.DictReader(io.StringIO(raw)):
  if r['symbol']==symbol:rows.append((int(r['start_ms']),int(r['end_ms'])))
 rows.sort();return rows
def epoch(x):
 v=int(float(x));return v//1000 if abs(v)>=10**15 else v
def main():
 a=argparse.ArgumentParser();a.add_argument('--symbol',required=True);a.add_argument('--month',required=True);a.add_argument('--windows',required=True);a.add_argument('--out',type=Path,required=True);z=a.parse_args();s=z.symbol.upper();z.out.mkdir(parents=True,exist_ok=True);ws=windows(z.windows,s,z.month);starts=[x[0] for x in ws]
 name=f'{s}-aggTrades-{z.month}.zip';url=f'{BASE}/{s}/{name}';arc=z.out/name;chk=z.out/(name+'.CHECKSUM');get(url+'.CHECKSUM',chk);get(url,arc);pub=checksum(chk,name);obs=sha(arc)
 if pub!=obs:raise ValueError(f'checksum {obs} != {pub}')
 out=z.out/f'{s}_{z.month}_windows.csv.gz';tot=sel=0;first=last=None;dups=back=0;prior=None
 with zipfile.ZipFile(arc) as zz,gzip.open(out,'wt',newline='',compresslevel=6) as f:
  member=[n for n in zz.namelist() if n.endswith('.csv')][0];rd=csv.reader(line.decode('utf-8-sig') for line in zz.open(member));wr=csv.writer(f);wr.writerow(COLS)
  for row in rd:
   if not row or row[0].lower().replace(' ','_') in {'agg_trade_id','aggtradeid'}:continue
   if len(row)<7:raise ValueError('short row')
   tot+=1;tid=int(float(row[0]));dups+=int(prior==tid);back+=int(prior is not None and tid<prior);prior=tid;t=epoch(row[5]);i=bisect.bisect_right(starts,t)-1
   if i<0 or not(ws[i][0]<=t<ws[i][1]):continue
   maker=row[6].strip().lower()
   if maker not in {'true','false'}:raise ValueError('maker flag')
   wr.writerow([tid,float(row[1]),float(row[2]),int(float(row[3])),int(float(row[4])),t,maker]);sel+=1;first=t if first is None else min(first,t);last=t if last is None else max(last,t)
 man={'status':'RESEARCH_ONLY','symbol':s,'month':z.month,'source_url':url,'published_sha256':pub,'observed_sha256':obs,'source_rows':tot,'selected_rows':sel,'first_ms':first,'last_ms':last,'duplicate_adjacent_ids':dups,'nonmonotonic_ids':back,'output':out.name,'output_sha256':sha(out),'orders_submitted':False};(z.out/'manifest.json').write_text(json.dumps(man,indent=2));arc.unlink();chk.unlink();print(json.dumps(man,indent=2))
if __name__=='__main__':main()
