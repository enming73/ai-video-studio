import pytest
from studio import director_vision as v

def test_normalize():
    assert v.normalize({'recommendation':'manual_review'})['action_match'] is None
    with pytest.raises(ValueError): v.normalize({'recommendation':'keep','visual_quality':6})
    with pytest.raises(ValueError): v.normalize({'recommendation':'delete'})

def test_budget_plan(monkeypatch):
    monkeypatch.setattr(v.director,'report',lambda pid,rid:{'shots':[
        {'seq':1,'frames':[{'file':'a.jpg'}],'status':'technical_pass'},
        {'seq':2,'frames':[],'status':'missing'}]})
    _,shots,estimate=v.plan('p','r',0.2,0.1)
    assert len(shots)==1 and not estimate['within_budget']
    _,shots,estimate=v.plan('p','r',0.2,0.2,{2})
    assert not shots and estimate['within_budget']
