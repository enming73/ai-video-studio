"""Enqueue legacy shots as persistent jobs and reconcile finished outputs."""
import json

import pytest

from studio import core, job_runner, jobs, ledger


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Runner project', 20, 'mock', 5)
    core.plan(pid, 10)
    run_id = ledger.open_run(pid, 5_000_000)
    return pid, run_id, tmp_path


def test_enqueue_is_idempotent_per_request_key(project):
    pid, run_id, _ = project
    first = job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                                    model='MiniMax-H3', request_key='shot-1')
    second = job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                                     model='MiniMax-H3', request_key='shot-1')
    assert first['id'] == second['id']
    assert len(jobs.list_for_project(pid)) == 1


def test_enqueue_rejects_unresolved_job_for_same_shot(project):
    pid, run_id, _ = project
    job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                            model='MiniMax-H3', request_key='one')
    with pytest.raises(ValueError, match='unresolved'):
        job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                                model='MiniMax-H3', request_key='two')


def test_completed_job_updates_shot(project):
    pid, run_id, root = project
    job = job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                                  model='MiniMax-H3', request_key='done')
    target = root / 'clip.mp4'
    target.write_bytes(b'video-bytes')
    jobs.set_status(job['id'], 'RUNNING')
    jobs.set_status(job['id'], 'SUCCEEDED', output={'file': str(target)})
    result = job_runner.reconcile(pid)
    assert result['updated'][0]['seq'] == 1
    with core.connect() as db:
        shot = db.execute('SELECT status,file FROM shots WHERE project_id=? AND seq=1',
                          (pid,)).fetchone()
        assert shot['status'] == 'COMPLETED'
        assert shot['file'] == str(target)
    assert len(job_runner.timeline.assets(pid, 1)) == 1


def test_reconcile_is_idempotent(project):
    pid, run_id, root = project
    job = job_runner.enqueue_shot(pid, 1, run_id=run_id, provider='minimax',
                                  model='MiniMax-H3', request_key='twice')
    target = root / 'clip.mp4'
    target.write_bytes(b'video-bytes')
    jobs.set_status(job['id'], 'RUNNING')
    jobs.set_status(job['id'], 'SUCCEEDED', output={'file': str(target)})
    job_runner.reconcile(pid)
    job_runner.reconcile(pid)
    assert len(job_runner.timeline.assets(pid, 1)) == 1


def test_open_or_reuse_run_is_idempotent(project):
    pid, _, _ = project
    first = job_runner.open_or_reuse_run(pid, request_key='run-key', budget_microusd=1_000_000)
    second = job_runner.open_or_reuse_run(pid, request_key='run-key', budget_microusd=1_000_000)
    assert first == second
