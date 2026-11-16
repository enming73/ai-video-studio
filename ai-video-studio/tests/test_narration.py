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


def _narration_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A friendly elephant story', 20, 'mock', 5)
    core.plan(pid, 10)
    return pid


def test_live_narration_cli_reports_job_status(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from studio.cli import app
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setattr(core, '_llm_json', lambda *a: {'lines': [
        {'seq': 1, 'text': '小象走进森林。'},
        {'seq': 2, 'text': '它遇见了朋友。'},
    ]})
    pid = _narration_project(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ['narration-plan', pid, '--live', '--budget', '1', '--model', 'test-model'])
    assert result.exit_code == 0
    assert 'job_id' in result.output or 'script' in result.output


def test_live_narration_cli_reports_blocked_job(tmp_path, monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from studio.cli import app
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setattr(core, '_llm_json',
                        lambda *a: (_ for _ in ()).throw(httpx.HTTPError('network down')))
    pid = _narration_project(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ['narration-plan', pid, '--live', '--budget', '1', '--model', 'test-model'])
    assert result.exit_code != 0
    assert 'blocked' in result.output.lower() or 'failed' in result.output.lower()
