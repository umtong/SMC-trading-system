from __future__ import annotations

import numpy as np
import pandas as pd

from research.wave39.wave39_engine_v3 import prior_atr_and_trends, simulate_stop_time_paths


def test_missing_minute_inside_holding_path_invalidates_trade():
    open_price=np.full(20,100.0); high=np.full(20,101.0); low=np.full(20,99.0)
    open_price[3]=np.nan; high[3]=np.nan; low[3]=np.nan
    gross,exits,stopped,entries,_=simulate_stop_time_paths(
        open_price,high,low,
        np.asarray([0],dtype=np.int64),
        np.asarray([1.0]),
        np.asarray([2,4],dtype=np.int64),
        np.asarray([3.0]),
        np.asarray([1],dtype=np.int64),
    )
    assert entries[0,0]==1
    assert np.isfinite(gross[0,0,0,0,0])
    assert np.isnan(gross[0,1,0,0,0])
    assert exits[0,1,0,0,0]==-1


def test_missing_entry_minute_rejects_all_paths():
    open_price=np.full(10,100.0); high=np.full(10,101.0); low=np.full(10,99.0)
    open_price[1]=np.nan; high[1]=np.nan; low[1]=np.nan
    gross,exits,_,entries,_=simulate_stop_time_paths(
        open_price,high,low,
        np.asarray([0],dtype=np.int64),
        np.asarray([1.0]),
        np.asarray([2],dtype=np.int64),
        np.asarray([3.0]),
        np.asarray([1],dtype=np.int64),
    )
    assert entries[0,0]==-1
    assert np.isnan(gross).all()
    assert (exits==-1).all()


def test_gap_resets_prior_atr_and_trend_availability():
    n=600
    clock=np.arange(n,dtype=np.int64)*60_000
    frame=pd.DataFrame({
        'open_time_ms':clock,
        'open':np.full(n,100.0),
        'high':np.full(n,101.0),
        'low':np.full(n,99.0),
        'close':np.linspace(100.0,101.0,n),
        'source_present':np.ones(n,dtype=np.int8),
    })
    frame.loc[300,['open','high','low','close']]=np.nan
    frame.loc[300,'source_present']=0
    boundaries=np.asarray([300*60_000, 550*60_000],dtype=np.int64)
    atr,trends,index=prior_atr_and_trends(frame,boundaries)
    assert index.tolist()==[300,550]
    assert np.isnan(atr[0])
    assert np.isnan(trends[240][0])
    assert np.isfinite(atr[1])
    assert np.isfinite(trends[240][1])
