from __future__ import annotations

import numpy as np

from research.wave39.wave39_engine_v4 import simulate_stop_time_paths


def test_stop_before_later_gap_remains_valid():
    open_price=np.full(12,100.0); high=np.full(12,101.0); low=np.full(12,99.0)
    low[2]=90.0
    open_price[4]=np.nan; high[4]=np.nan; low[4]=np.nan
    gross,exits,stopped,entries,_=simulate_stop_time_paths(
        open_price,high,low,
        np.asarray([0],dtype=np.int64),np.asarray([1.0]),
        np.asarray([3,6],dtype=np.int64),np.asarray([3.0]),np.asarray([1],dtype=np.int64),
    )
    assert entries[0,0]==1
    assert stopped[0,0,0,0,0]==1
    assert stopped[0,1,0,0,0]==1
    assert exits[0,0,0,0,0]==2
    assert exits[0,1,0,0,0]==2
    assert np.isfinite(gross[0,0,0,0,0])
    assert np.isfinite(gross[0,1,0,0,0])


def test_time_exit_after_gap_is_invalid():
    open_price=np.full(12,100.0); high=np.full(12,101.0); low=np.full(12,99.0)
    open_price[4]=np.nan; high[4]=np.nan; low[4]=np.nan
    gross,exits,stopped,_,_=simulate_stop_time_paths(
        open_price,high,low,
        np.asarray([0],dtype=np.int64),np.asarray([1.0]),
        np.asarray([2,6],dtype=np.int64),np.asarray([5.0]),np.asarray([1],dtype=np.int64),
    )
    assert np.isfinite(gross[0,0,0,0,0])
    assert exits[0,0,0,0,0]==3
    assert np.isnan(gross[0,1,0,0,0])
    assert exits[0,1,0,0,0]==-1
    assert stopped[0,1,0,0,0]==0
