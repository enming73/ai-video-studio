import sqlite3
import pytest
from benchmark.core import connect,plan,total_reserved,PROMPTS,report
from benchmark.providers import MiniMax,Flux,Wan,Poll,ProviderError

class FakeResponse:
    def __init__(self,data): self.data=data
    def raise_for_status(self): pass
    def json(self): return self.data
class FakeClient:
    def __init__(self,responses): self.responses=iter(responses);self.calls=[]
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs));return FakeResponse(next(self.responses))

def test_plan_idempotent(tmp_path,monkeypatch):
    monkeypatch.setenv('WAN_USD_PER_SECOND','0.2');monkeypatch.setenv('FAL_USD_PER_SECOND','0.17')
    db=connect(tmp_path/'test.sqlite3')
    assert plan(db,['minimax','flux','wan'],['T01'],1,5)==3
    assert plan(db,['minimax','flux','wan'],['T01'],1,5)==0
    assert total_reserved(db)==0
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==3
    assert 'Video Benchmark' in report(db,tmp_path/'report.html').read_text()

def test_minimax(monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY','fake')
    client=FakeClient([{'task_id':'123'},{'task':{'status':'succeeded','content':{'url':'https://x'}}}])
    p=MiniMax(client)
    assert p.submit('hello',5)=='123'
    assert p.poll('123')==Poll('succeeded','https://x')
    assert client.calls[0][2]['json']['model']=='MiniMax-H3'

def test_flux(monkeypatch):
    monkeypatch.setenv('FAL_KEY','fake')
    client=FakeClient([{'request_id':'abc'},{'status':'COMPLETED'},{'video':{'url':'https://x'}}])
    p=Flux(client)
    assert p.submit('hello',5)=='abc'
    assert p.poll('abc').url=='https://x'

def test_wan(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY','fake')
    monkeypatch.setenv('WAN_BASE_URL','https://workspace.eu-central-1.maas.aliyuncs.com')
    client=FakeClient([{'output':{'task_id':'123'}},{'output':{'task_status':'SUCCEEDED','video_url':'https://x'}}])
    p=Wan(client)
    assert p.submit('hello',5)=='123'
    assert p.poll('123').url=='https://x'

def test_wan_untrusted(monkeypatch):
    monkeypatch.setenv('WAN_BASE_URL','https://wan3.video')
    with pytest.raises(ProviderError): Wan(FakeClient([]))

def test_flux_poll_uses_model_queue_url(monkeypatch):
    monkeypatch.setenv('FAL_KEY','fake')
    client=FakeClient([{'status':'IN_PROGRESS'}])
    assert Flux(client).poll('uuid').status=='running'
    assert client.calls[0][1]=='https://queue.fal.run/blackforestlabs/flux-3-max/text-to-video/requests/uuid/status'


def test_flux_duration_sent_as_integer(monkeypatch):
    monkeypatch.setenv('FAL_KEY','fake')
    client=FakeClient([{'request_id':'abc'}])
    Flux(client).submit('hello',5)
    payload=client.calls[0][2]['json']
    assert payload['duration'] == 5
    assert type(payload['duration']) is int

def test_run_job_id_only(tmp_path,monkeypatch,capsys):
    from benchmark.core import run
    monkeypatch.setenv('FAL_USD_PER_SECOND','0.17')
    db=connect(tmp_path/'test.sqlite3')
    plan(db,['flux'],['T01','T03'],2,5)
    run(db,False,20,job_id='flux:T01:2')
    output=capsys.readouterr().out
    assert 'flux:T01:2' in output
    assert 'flux:T01:1' not in output
    assert 'flux:T03:1' not in output
    with pytest.raises(ValueError,match='Unknown job ID'):
        run(db,False,20,job_id='flux:T99:1')
    db.execute("UPDATE jobs SET status='failed' WHERE id='flux:T01:2'")
    db.commit()
    with pytest.raises(ValueError,match='not runnable'):
        run(db,False,20,job_id='flux:T01:2')
