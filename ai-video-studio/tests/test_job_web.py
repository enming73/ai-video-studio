"""Embedded worker and persistent job API."""
import threading
import time

import pytest

from studio import core, jobs, ledger, worker


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Job API', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    return pid, run_id


class Handler:
    def __init__(self):
        self.calls = 0

    def execute(self, job, attempts, control):
        self.calls += 1
        return {'ok': True}

    def resume(self, job, attempt, control):
        self.calls += 1
        return {'ok': True}


def test_embedded_worker_starts_once_and_stops():
    first = worker.start_embedded(poll_seconds=0.01)
    second = worker.start_embedded(poll_seconds=0.01)
    assert first is second and first.is_alive()
    worker.stop_embedded()
    assert not first.is_alive()


def test_embedded_worker_processes_queued_job(project):
    pid, run_id = project
    job = jobs.create(pid, kind='video', payload={}, request_key='embedded',
                      run_id=run_id)
    handler = Handler()
    worker.register('video', handler)
    worker.start_embedded(poll_seconds=0.01)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and jobs.get(job['id'])['status'] != 'SUCCEEDED':
            time.sleep(0.02)
    finally:
        worker.stop_embedded()
    assert jobs.get(job['id'])['status'] == 'SUCCEEDED'
    assert handler.calls == 1


def test_disable_embedded_worker_env(monkeypatch):
    from fastapi.testclient import TestClient
    from studio import webapp
    monkeypatch.setenv('STUDIO_DISABLE_EMBEDDED_WORKER', '1')
    worker.stop_embedded()
    with TestClient(webapp.app):
        assert worker._embedded_thread is None


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from studio import webapp
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('STUDIO_DISABLE_EMBEDDED_WORKER', '1')
    return TestClient(webapp.app)


def cloud_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    pid = core.create('Cloud project', 20, 'minimax', 5)
    core.plan(pid, 10)
    return pid


def test_production_generate_creates_persistent_jobs(api, tmp_path, monkeypatch):
    pid = cloud_project(tmp_path, monkeypatch)
    response = api.post(f'/api/projects/{pid}/production/generate', json={
        'live': True, 'budget': 5, 'confirm_paid': True,
        'confirmation_project_id': pid, 'limit': 0})
    assert response.status_code == 202, response.text
    data = response.json()
    assert data['status'] == 'queued'
    assert len(data['job_ids']) == 2
    assert len(jobs.list_for_project(pid)) == 2


def test_dry_run_does_not_create_jobs(api, tmp_path, monkeypatch):
    pid = cloud_project(tmp_path, monkeypatch)
    response = api.post(f'/api/projects/{pid}/production/generate', json={
        'live': False, 'budget': 5})
    assert response.status_code == 202
    assert jobs.list_for_project(pid) == []


def test_production_status_survives_restart(api, tmp_path, monkeypatch):
    pid = cloud_project(tmp_path, monkeypatch)
    created = api.post(f'/api/projects/{pid}/production/generate', json={
        'live': True, 'budget': 5, 'confirm_paid': True,
        'confirmation_project_id': pid}).json()
    run_id = created['run_id']
    from fastapi.testclient import TestClient
    from studio import webapp
    api.close()
    api = TestClient(webapp.app)
    status = api.get(f'/api/projects/{pid}/production/jobs/{run_id}')
    assert status.status_code == 200
    assert status.json()['run_id'] == run_id
    assert len(status.json()['jobs']) == 2


def make_job(pid, run_id, key='api'):
    return jobs.create(pid, kind='video', payload={}, request_key=key, run_id=run_id)


def test_job_api_lists_and_gets(api, project):
    pid, run_id = project
    job = make_job(pid, run_id)
    listed = api.get(f'/api/projects/{pid}/jobs')
    assert listed.status_code == 200
    assert listed.json()[0]['id'] == job['id']
    detail = api.get(f"/api/jobs/{job['id']}")
    assert detail.status_code == 200
    assert detail.json()['attempts'] == []
    assert api.get('/api/jobs/unknown').status_code == 404


def test_job_api_pause_resume_cancel(api, project):
    pid, run_id = project
    job = make_job(pid, run_id, 'control')
    assert api.post(f"/api/jobs/{job['id']}/pause").json()['status'] == 'PAUSED'
    assert api.post(f"/api/jobs/{job['id']}/resume").json()['status'] == 'PENDING'
    assert api.post(f"/api/jobs/{job['id']}/cancel").json()['status'] == 'CANCELLED'
    assert api.post(f"/api/jobs/{job['id']}/pause").status_code == 409


def test_costs_endpoint_exposes_unknown_and_committed(api, project):
    pid, run_id = project
    ledger.reserve(pid, stage='video', provider='fake', model='m',
                   amount_microusd=100_000, pricing_version='v1', run_id=run_id)
    data = api.get(f'/api/projects/{pid}/costs').json()
    assert data['committed_microusd'] == 100_000
    assert data['open_exposure_microusd'] == 100_000
    assert 'not a provider invoice' in data['note']


def test_duplicate_post_with_idempotency_key_reuses_run(api, tmp_path, monkeypatch):
    pid = cloud_project(tmp_path, monkeypatch)
    body = {'live': True, 'budget': 5, 'confirm_paid': True,
            'confirmation_project_id': pid}
    first = api.post(f'/api/projects/{pid}/production/generate', json=body,
                     headers={'Idempotency-Key': 'same'})
    second = api.post(f'/api/projects/{pid}/production/generate', json=body,
                      headers={'Idempotency-Key': 'same'})
    assert first.json()['job_ids'] == second.json()['job_ids']
    assert len(jobs.list_for_project(pid)) == 2


def test_production_job_by_job_id_serializes(api, tmp_path, monkeypatch):
    pid = cloud_project(tmp_path, monkeypatch)
    created = api.post(f'/api/projects/{pid}/production/generate', json={
        'live': True, 'budget': 5, 'confirm_paid': True,
        'confirmation_project_id': pid}).json()
    job_id = created['job_ids'][0]
    response = api.get(f'/api/projects/{pid}/production/jobs/{job_id}')
    assert response.status_code == 200
    assert response.json()['jobs'][0]['id'] == job_id


def test_unknown_project_jobs_returns_404(api):
    assert api.get('/api/projects/nope/jobs').status_code == 404
