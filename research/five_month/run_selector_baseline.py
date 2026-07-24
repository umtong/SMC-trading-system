from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pandas as pd


def load_engine(path: Path):
    spec=importlib.util.spec_from_file_location('five_month_selector_engine',path)
    if spec is None or spec.loader is None:raise RuntimeError(path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def main():
    p=argparse.ArgumentParser();p.add_argument('--engine',type=Path,required=True);p.add_argument('--ledger',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--policy-trials',type=int,default=0);p.add_argument('--seed',type=int,default=20260722);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    e=load_engine(a.engine);events=pd.read_pickle(a.ledger).sort_values(['known','symbol']).reset_index(drop=True)
    events=e.walk_forward_predictions(events,a.seed);fcols=e.feature_columns(events);audit=e.leakage_audit(events,fcols)
    if not audit['passed']:raise RuntimeError(f'leakage audit failed: {audit}')
    events.to_pickle(a.output/'event_ledger.pkl.gz',compression='gzip');(a.output/'leakage_audit.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
    selected,search=e.policy_search(events,a.policy_trials,a.seed)
    hold_s,hold_e,_=e.window_manifest('2025-01-01','2026-02-01',1);all_s,all_e,_=e.window_manifest('2022-01-01','2026-02-01',1)
    selected['holdout']=e.evaluate_cfg(events,selected['cfg'],hold_s,hold_e);selected['all_daily_starts']=e.evaluate_cfg(events,selected['cfg'],all_s,all_e)
    selected['five_month_gate']={'required_multiple':5.0,'required_success_rate':.99,'required_trades_more_than_days':True,'calendar_months':5,'passed':bool(selected['holdout']['success_rate']>=.99 and selected['holdout']['p01']>=5 and selected['holdout']['frequency_margin_p01']>0)}
    selected['leakage_audit']=audit;selected['automatic_update_contract']={'retrain_frequency':'monthly','training_window_fast_days':730,'training_window_slow_days':1095,'recency_half_life_fast_days':180,'recency_half_life_slow_days':365,'embargo':'labels complete before month start','rank_calibration':'strictly earlier predictions only','promotion':'development -> validation -> frozen holdout -> shadow; no automatic live promotion'}
    search.to_csv(a.output/'policy_search.csv',index=False);(a.output/'summary.json').write_text(json.dumps(selected,indent=2),encoding='utf-8');print(json.dumps(selected,indent=2))
if __name__=='__main__':main()
