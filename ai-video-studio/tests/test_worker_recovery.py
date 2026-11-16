"""A killed submission must never be submitted again on restart."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from video_provider_core import CostEstimate, RemoteJob, RemoteStatus

from studio import attempts, core, jobs, ledger, worker
from studio.handlers import video


CHILD = r'''
import os, time
from pathlib import Path
from studio import core, jobs, ledger, worker
from studio.handlers import video
from video_provider_core import CostEstimate, RemoteJob

core.HOME = Path(os.environ['HOME_DIR'])
pid = core.create('crash project', 20, 'mock', 5)
run_id = ledger.open_run(pid, 5_000_000)
job = jobs.create(pid, kind='video', payload={
    'prompt': 'waves', 'duration_sec': 5, 'width': 1280, 'height': 720,
    'provider': 'fake', 'model': 'm', 'poll_interval_sec': 0,
}, request_key='crash', run_id=run_id)
Path(os.environ['JOB_FILE']).write_text(job['id'])

class P:
    def __init__(self, client): pass
    def estimate_cost(self, request): return CostEstimate(10_000, 'test', 'test')
    def submit_video(self, request, key):
        Path(os.environ['MARKER']).write_text('submitted')
        time.sleep(60)

video.PROVIDERS['fake'] = P
worker.register('video', video)
worker.run_once('child')
'''


def test_killed_submitting_job_is_not_resubmitted(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    marker = tmp_path / 'marker'
    job_file = tmp_path / 'job'
    env = os.environ.copy()
    env.update(HOME_DIR=str(home), MARKER=str(marker), JOB_FILE=str(job_file),
               STUDIO_HOME=str(home))
    proc = subprocess.Popen([sys.executable, '-c', CHILD], env=env,
                            cwd=str(Path(__file__).parents[1]))
    try:
        deadline = time.time() + 20
        while not marker.exists() and time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f'child exited early: {proc.returncode}')
            time.sleep(0.05)
        assert marker.exists(), 'child never reached submit'
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    monkeypatch.setattr(core, 'HOME', home)
    job_id = job_file.read_text()
    with core.connect() as db:
        db.execute("UPDATE jobs SET lease_expires='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (job_id,))
    submits = []

    class P:
        def __init__(self, client): pass
        def estimate_cost(self, request): return CostEstimate(10_000, 'test', 'test')
        def submit_video(self, request, key):
            submits.append(key)
            return RemoteJob('fake', 'again')
        def status(self, job, *, model=None):
            return RemoteStatus('succeeded', url='https://fal.media/v.mp4')

    video.PROVIDERS['fake'] = P
    worker.register('video', video)
    worker.run_once('parent')
    assert submits == []
    saved = jobs.get(job_id)
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    assert jobs.latest_attempt(job_id)['status'] == 'UNKNOWN_REMOTE_STATE'


def test_completed_attempt_marks_job_succeeded_without_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('done project', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    job = jobs.create(pid, kind='video', payload={}, request_key='done', run_id=run_id)
    attempt = attempts.create(job['id'], provider='fake', model='m', input_data={})
    attempts.prepare_submission(attempt['id'], amount_microusd=10_000, pricing_version='v1')
    attempts.attach_remote(attempt['id'], 'remote')
    attempts.mark_status(attempt['id'], 'POLLING')
    attempts.mark_status(attempt['id'], 'DOWNLOADING')
    attempts.mark_status(attempt['id'], 'VALIDATING')
    attempts.mark_completed(attempt['id'], {'file': '/tmp/done.mp4'})
    calls = []
    class H:
        def execute(self, job, service, control): calls.append('execute'); return {}
        def resume(self, job, attempt, control): calls.append('resume'); return {}
    worker.register('video', H())
    worker.run_once('w')
    assert calls == []
    assert jobs.get(job['id'])['status'] == 'SUCCEEDED'


def test_cli_jobs_and_controls(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from studio.cli import app
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('cli project', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    job = jobs.create(pid, kind='video', payload={}, request_key='cli', run_id=run_id)
    runner = CliRunner()
    listed = runner.invoke(app, ['jobs', pid])
    assert listed.exit_code == 0 and job['id'] in listed.stdout
    assert runner.invoke(app, ['job', job['id']]).exit_code == 0
    assert runner.invoke(app, ['pause', job['id']]).exit_code == 0
    assert runner.invoke(app, ['resume', job['id']]).exit_code == 0
    assert runner.invoke(app, ['cancel', job['id']]).exit_code == 0
    assert jobs.get(job['id'])['status'] == 'CANCELLED'
