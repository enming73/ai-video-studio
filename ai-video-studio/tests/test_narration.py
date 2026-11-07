import json
import pytest
from studio import core, narration


def test_narration_dry_run_and_script(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid=core.create('A friendly elephant story',20,'mock',0)
    core.plan(pid,10)
    assert narration.plan(pid,model='example')['dry_run']
    assert not (tmp_path/pid/'narration'/'script.json').exists()
    path=tmp_path/pid/'narration'/'script.json'
    path.write_text(json.dumps({'project_id':pid,'language':'zh-CN','lines':[
        {'seq':1,'duration':10,'text':'小象走进森林。'},
        {'seq':2,'duration':10,'text':'它遇见了朋友。'}]},ensure_ascii=False),encoding='utf-8')
    assert len(narration.show(pid)['lines'])==2
    assert narration.synthesize(pid)['requests']==2
    assert not (tmp_path/pid/'narration'/'narration.wav').exists()
    srt=tmp_path/'sub.srt'
    narration._srt(narration.show(pid)['lines'],srt)
    assert '00:00:10,000 --> 00:00:20,000' in srt.read_text()


def test_narration_rejects_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid=core.create('test',10,'mock',0);core.plan(pid,10)
    path=narration._script_path(pid)
    path.write_text(json.dumps({'lines':[{'seq':2,'duration':10,'text':'wrong'}]}))
    with pytest.raises(ValueError,match='Invalid narration'):
        narration.show(pid)
