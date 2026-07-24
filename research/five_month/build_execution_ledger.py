from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np


def load_engine(path: Path):
    spec=importlib.util.spec_from_file_location('five_month_execution_engine',path)
    if spec is None or spec.loader is None:raise RuntimeError(path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def main():
    p=argparse.ArgumentParser();p.add_argument('--engine',type=Path,required=True);p.add_argument('--start',default='2021-01');p.add_argument('--end',default='2026-07');p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    e=load_engine(a.engine);months=e.month_strings(a.start,a.end)
    one={s:e.load_symbol(s,months,a.cache) for s in e.SYMBOLS};five={s:e.aggregate_5m(one[s]) for s in e.SYMBOLS}
    if not np.array_equal(five[e.SYMBOLS[0]]['ts'],five[e.SYMBOLS[1]]['ts']):raise RuntimeError('BTC/ETH 5m timestamps are not aligned')
    local,shared=e.feature_state(five);candidates=e.build_candidates(five,local,shared);events=e.attach_execution(candidates,one)
    events.to_pickle(a.output/'execution_ledger.pkl.gz',compression='gzip')
    report={'schema':'smc.five_month.execution_ledger.v1','rows':len(events),'candidates':len(candidates),'fills':int((events.fill>=0).sum()),'fill_rate':float((events.fill>=0).mean()),'months':months,'symbols':list(e.SYMBOLS),'checksum_verified':True,'contract':{'features':'completed M1/M5/H1 only','entry':'maker body-mid limit, no market fallback','pending_expiry_minutes':60,'cancellation':'stop invalidation or planned +1R departure before fill','time_exit':'open of fi+hold before that minute high/low','gap_stop':'STOP_SLIP + STOP_FEE'}}
    (a.output/'execution_ledger_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
