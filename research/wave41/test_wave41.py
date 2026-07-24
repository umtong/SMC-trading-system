from __future__ import annotations

import numpy as np

from research.wave41 import run_wave41_development as base
from research.wave41 import run_wave41_development_v2 as v2


def test_rolling_beta_is_future_mutation_invariant():
    rng=np.random.default_rng(7)
    factor=rng.normal(size=8000)
    target=0.7*factor+rng.normal(scale=0.4,size=8000)
    original=v2.rolling_beta_v2(target,factor)
    changed_target=target.copy(); changed_factor=factor.copy()
    changed_target[7000:]+=1000.0; changed_factor[7000:]-=1000.0
    changed=v2.rolling_beta_v2(changed_target,changed_factor)
    np.testing.assert_allclose(original[:7000],changed[:7000],equal_nan=True)


def test_argmax_tie_break_uses_registered_symbol_order():
    n=6000
    state={
        'residual_flow':np.ones((n,4)),
        'residual_return':np.ones((n,4)),
    }
    selected,side,score=base.choose_family('IDIOSYNCRATIC_FLOW_FOLLOW',1.0,state)
    assert np.all(selected==0)
    assert np.all(side==1)
    assert np.all(score==1.0)


def test_underreaction_uses_flow_direction():
    state={
        'residual_flow':np.asarray([[2.0,-3.0,0.5,0.1]]),
        'residual_return':np.asarray([[0.2,-0.1,1.0,0.1]]),
    }
    selected,side,score=base.choose_family('FLOW_UNDERREACTION_CATCHUP',1.0,state)
    assert selected.tolist()==[1]
    assert side.tolist()==[-1]
    assert np.isclose(score[0],2.9)


def test_unsupported_dislocation_excludes_btc():
    state={
        'residual_flow':np.asarray([[0.0,0.0,0.0,0.0]]),
        'residual_return':np.asarray([[100.0,1.0,2.0,3.0]]),
    }
    selected,side,score=base.choose_family('UNSUPPORTED_RELATIVE_DISLOCATION',1.0,state)
    assert selected.tolist()==[3]
    assert side.tolist()==[-1]
    assert np.isclose(score[0],3.0)
