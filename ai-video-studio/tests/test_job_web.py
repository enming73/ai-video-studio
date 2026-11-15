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
