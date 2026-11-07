import json
import pytest
from studio import continuity_vision as cv


def test_normalize():
    good={'severity':'medium','confidence':0.8,'recommendation':'revise_next_opening','observations':['new frog'],'uncertainties':['only sampled frames'],'reason':'abrupt','proposed_prompt':'frog emerges from leaves'}
    assert cv.normalize(good)['proposed_prompt'].startswith('frog')
    with pytest.raises(ValueError): cv.normalize({**good,'confidence':2})
    with pytest.raises(ValueError): cv.normalize({**good,'recommendation':'pay_now'})


def test_dry_run_no_calls(monkeypatch):
    monkeypatch.setattr(cv.continuity,'report',lambda pid,rid:{'boundaries':[
        {'from_seq':2,'to_seq':3,'status':'ready','frames':{'out':[{},{}],'in':[{},{}]}}
    ]})
    r=cv.review('p','abcdef123456','model',pairs=[2],budget_usd=0)
    assert r['paid_api_calls']==0 and r['pairs'][0]['to_seq']==3
    assert not r['within_budget']
    with pytest.raises(ValueError,match='Budget insufficient'):
        cv.review('p','abcdef123456','model',live=True,budget_usd=0,api_key='fake')


def test_frame_path_restriction(tmp_path):
    root=tmp_path/'safe';root.mkdir()
    outside=tmp_path/'secret.jpg';outside.write_bytes(b'secret')
    with pytest.raises(ValueError): cv._frame({'file':str(outside)},root)
