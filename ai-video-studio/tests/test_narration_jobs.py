"""Narration planning Job tests: enqueue, stale-snapshot guard, worker writes."""
import httpx
import pytest

from studio import core, jobs, narration, worker
from studio import narration_jobs

SCRIPT_LLM = {'lines': [
    {'seq': 1, 'text': '小象走进森林。'},
    {'seq': 2, 'text': '它遇见了朋友。'},
]}


@pytest.fixture(autouse=True)
def llm_env(monkeypatch):
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')


@pytest.fixture(autouse=True)
def handlers():
    from studio.handlers import register_all
    register_all()


@pytest.fixture
def narration_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A friendly elephant story', 20, 'mock', 5)
    core.plan(pid, 10)
    return pid


@pytest.fixture
def narration_job(narration_project):
    queued = narration_jobs.enqueue_plan(
        narration_project, language='zh-CN', model='test-model',
        base_url='https://api.example.com/v1', budget_usd=1, request_id='n1')
    return {'pid': narration_project, 'job_id': queued['job_id']}


def test_narration_plan_runs_through_job(narration_project, monkeypatch):
    pid = narration_project
    monkeypatch.setattr(core, '_llm_json', lambda *a: SCRIPT_LLM)
    result = narration.plan(pid, model='test-model', live=True, budget_usd=1,
                            request_id='n1')
    assert result['job_id']
    assert result['script']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE kind=?',
                          ('llm_narration',)).fetchone()[0] == 1


def test_narration_plan_stale_storyboard_fails_without_overwrite(narration_project, monkeypatch):
    pid = narration_project
    monkeypatch.setattr(core, '_llm_json', lambda *a: SCRIPT_LLM)
    queued = narration_jobs.enqueue_plan(
        pid, language='zh-CN', model='test-model',
        base_url='https://api.example.com/v1', budget_usd=1, request_id='n2')
    with core.connect() as db:
        db.execute('UPDATE shots SET duration=? WHERE project_id=? AND seq=?', (8, pid, 1))
    worker.run_once()
    assert jobs.get(queued['job_id'])['status'] == 'FAILED'
    assert not narration._script_path(pid).exists()


def test_narration_worker_writes_script(narration_job, monkeypatch):
    pid = narration_job['pid']
    script = {
        'project_id': pid, 'language': 'zh-CN', 'model': 'test-model',
        'lines': [
            {'seq': 1, 'duration': 10, 'text': '小象走进森林。'},
            {'seq': 2, 'duration': 10, 'text': '它遇见了朋友。'},
        ],
    }
    monkeypatch.setattr(narration, 'generate_script', lambda payload: script)
    assert worker.run_once() == narration_job['job_id']
    assert narration.show(pid)['lines'] == script['lines']
    assert jobs.get(narration_job['job_id'])['status'] == 'SUCCEEDED'


def test_narration_fails_fast_without_llm_key(narration_project, monkeypatch):
    from studio import guided_storyboard
    pid = narration_project
    monkeypatch.setattr(guided_storyboard, 'load_dotenv', lambda: None)
    monkeypatch.delenv('STUDIO_LLM_API_KEY', raising=False)
    monkeypatch.setattr(core, '_llm_json', lambda *a: pytest.fail('no request without a key'))
    queued = narration_jobs.enqueue_plan(pid, language='zh-CN', model='test-model',
                                          base_url='https://api.example.com/v1',
                                          budget_usd=1, request_id='no-key')
    worker.run_once()
    assert jobs.get(queued['job_id'])['status'] == 'FAILED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM job_attempts WHERE job_id=?',
                          (queued['job_id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM cost_entries WHERE job_id=?',
                          (queued['job_id'],)).fetchone()[0] == 0


def test_narration_unknown_remote_blocks_and_retains_reservation(narration_project, monkeypatch):
    pid = narration_project
    monkeypatch.setattr(core, '_llm_json',
                        lambda *a: (_ for _ in ()).throw(httpx.HTTPError('network down')))
    queued = narration_jobs.enqueue_plan(pid, language='zh-CN', model='test-model',
                                          base_url='https://api.example.com/v1',
                                          budget_usd=1, request_id='unknown')
    worker.run_once()
    saved = jobs.get(queued['job_id'])
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (queued['job_id'],)).fetchone()[0] == 'reserved'
    assert not narration._script_path(pid).exists()
