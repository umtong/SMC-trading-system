from __future__ import annotations
import argparse, hashlib, json, re, traceback
from pathlib import Path
import pandas as pd

REPO='predict-quant/binance-future-orderbook'
SYMS=('BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT')
DATE_RE=re.compile(r'(20\d{2}-\d{2}-\d{2})_([A-Z0-9]+)_depth20\.parquet$')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--cache',type=Path,default=Path('.cache/v19-lob')); ap.add_argument('--output',type=Path,default=Path('artifacts/v19-lob-inspect')); a=ap.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    try:
        from huggingface_hub import HfApi, hf_hub_download
        api=HfApi(); files=api.list_repo_files(REPO,repo_type='dataset')
        by={s:{} for s in SYMS}
        for f in files:
            m=DATE_RE.search(f)
            if not m: continue
            d,s=m.groups()
            if s in by: by[s][d]=f
        common=sorted(set.intersection(*(set(by[s]) for s in SYMS)))
        chosen={s:(common[-1] if common else sorted(by[s])[-1]) for s in SYMS}
        report={'repo':REPO,'file_count':len(files),'counts':{s:len(by[s]) for s in SYMS},'ranges':{s:[min(by[s]),max(by[s])] if by[s] else None for s in SYMS},'common_dates':common,'chosen':chosen,'files':{}}
        for s,d in chosen.items():
            rp=by[s][d]
            lp=Path(hf_hub_download(REPO,rp,repo_type='dataset',cache_dir=a.cache))
            df=pd.read_parquet(lp)
            sample=df.head(1000).copy()
            stats={'repo_path':rp,'local_path':str(lp),'sha256':hashlib.sha256(lp.read_bytes()).hexdigest(),'bytes':lp.stat().st_size,'rows':len(df),'columns':list(df.columns),'dtypes':{c:str(t) for c,t in df.dtypes.items()},'head':json.loads(sample.head(3).to_json(orient='records',date_format='iso'))}
            for c in df.columns:
                if pd.api.types.is_numeric_dtype(df[c]):
                    x=pd.to_numeric(df[c],errors='coerce')
                    stats.setdefault('numeric',{})[c]={'min':None if x.dropna().empty else float(x.min()),'max':None if x.dropna().empty else float(x.max()),'nan':int(x.isna().sum())}
            report['files'][s]=stats
            sample.to_parquet(a.output/f'{s}_{d}_sample1000.parquet',index=False)
            del df
        (a.output/'schema_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        (a.output/'summary.md').write_text('\n'.join([
            '# V19 order-book schema inspection','',f"Repo: `{REPO}`",f"Common dates: `{len(common)}`",f"Chosen: `{chosen}`",'',
            *[f"- {s}: {report['files'][s]['rows']:,} rows, {report['files'][s]['bytes']/1e6:.1f} MB, columns={report['files'][s]['columns']}" for s in SYMS]
        ]),encoding='utf-8')
        print(json.dumps({'common_dates':len(common),'chosen':chosen,'counts':report['counts']},indent=2)); return 0
    except Exception:
        tb=traceback.format_exc(); (a.output/'failure.txt').write_text(tb,encoding='utf-8'); print(tb); return 1
if __name__=='__main__': raise SystemExit(main())
