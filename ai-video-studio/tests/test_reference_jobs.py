"""Reference-image Job tests: enqueue, replay idempotency, worker import, and
the shared image exception split (pre-request release, post-response retain,
unknown-remote block).

These tests never call a real image API. They monkeypatch the extracted
``image_jobs._request_edit`` to return local PNG bytes, or inject a fault so the
request never leaves the process.
"""
import base64
import json
from pathlib import Path

import pytest

from studio import core, image_jobs, jobs, references, reference_generation as rg, worker

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aZkAAAAASUVORK5CYII=')


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')


@pytest.fixture(autouse=True)
def _handlers():
    from studio.handlers import register_all
    register_all()


@pytest.fixture
def reference_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Luma the elephant', 10, 'minimax', 5)
    with core.connect() as db:
        db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)',
                   (pid + '-s001', pid, 1, 'Luma walks in jungle', 10, 'PLANNED'))
        db.execute('INSERT INTO storyboards(project_id,bible_json,planner,updated) VALUES (?,?,?,?)',
                   (pid, json.dumps({'Luma': 'gray elephant'}), 'test', 'now'))
    f = tmp_path / 'ref.png'
    f.write_bytes(PNG)
    references.import_character(pid, 'Luma', f)
    return pid


@pytest.fixture
def reference_job(reference_project):
    queued = image_jobs.enqueue_reference(
        reference_project, 1, rg.DEFAULT_IMAGE_MODEL, 1,
        confirm_paid=True, request_id='w1', force=False)
    return {'pid': reference_project, 'job_id': queued['job_id']}


def test_reference_frame_runs_through_job(reference_project, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_edit', lambda *a, **k: PNG)
    result = rg.generate_frame(reference_project, 1, live=True, budget_usd=1,
                               request_id='r1')
    assert result['job_id']
    assert references.reference_manifest(reference_project)['shots']['1']['file']


def test_reference_frame_replay_returns_same_job(reference_project, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_edit', lambda *a, **k: PNG)
    first = rg.generate_frame(reference_project, 1, live=True, budget_usd=1,
                              request_id='r1')
    second = rg.generate_frame(reference_project, 1, live=True, budget_usd=1,
                               request_id='r1')
    assert second['job_id'] == first['job_id']


def test_reference_worker_imports_generated_shot(reference_job, monkeypatch):
    pid = reference_job['pid']
    monkeypatch.setattr(image_jobs, '_request_edit', lambda *a, **k: PNG)
    assert worker.run_once() == reference_job['job_id']
    assert references.reference_manifest(pid)['shots']['1']['file']
    assert jobs.get(reference_job['job_id'])['status'] == 'SUCCEEDED'


def test_reference_missing_key_fails_and_releases(reference_job, monkeypatch):
    from studio import jobs as jobs_module
    monkeypatch.setattr(image_jobs, 'load_dotenv', lambda: None)
    monkeypatch.delenv('STUDIO_IMAGE_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    assert worker.run_once() == reference_job['job_id']
    assert jobs_module.get(reference_job['job_id'])['status'] == 'FAILED'
    with core.connect() as db:
        entry = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                           (reference_job['job_id'],)).fetchone()
        assert entry is not None and entry['billing_status'] == 'released'


def test_reference_invalid_image_retains_reservation(reference_job, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_edit', lambda *a, **k: b'not a png')
    assert worker.run_once() == reference_job['job_id']
    assert jobs.get(reference_job['job_id'])['status'] == 'FAILED'
    with core.connect() as db:
        entry = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                           (reference_job['job_id'],)).fetchone()
        assert entry['billing_status'] == 'reserved'
        attempt = db.execute('SELECT status, error_class FROM job_attempts WHERE job_id=?',
                             (reference_job['job_id'],)).fetchone()
        assert attempt['status'] == 'FAILED_FINAL'
        assert attempt['error_class'] == 'content_rejected'


def test_reference_unknown_remote_blocks_and_retains(reference_job, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_edit',
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    assert worker.run_once() == reference_job['job_id']
    saved = jobs.get(reference_job['job_id'])
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (reference_job['job_id'],)).fetchone()[0] == 'reserved'


def test_reference_force_requires_explicit_request_id(reference_project, monkeypatch):
    with pytest.raises(ValueError, match='--request-id'):
        rg.generate_frame(reference_project, 1, live=True, budget_usd=1, force=True)
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0] == 0
    # With an explicit request id, --force mints a new Job rather than replaying.
    monkeypatch.setattr(image_jobs, '_request_edit', lambda *a, **k: PNG)
    first = rg.generate_frame(reference_project, 1, live=True, budget_usd=1,
                              request_id='v1')
    second = rg.generate_frame(reference_project, 1, live=True, budget_usd=1,
                               request_id='v2', force=True)
    assert second['job_id'] != first['job_id']


def test_reference_missing_file_releases_reservation(reference_job, monkeypatch):
    pid = reference_job['pid']
    called = []
    monkeypatch.setattr(image_jobs, '_request_edit',
                        lambda *a, **k: called.append(True) or PNG)
    with core.connect() as db:
        payload = json.loads(db.execute(
            'SELECT payload_json FROM jobs WHERE id=?',
            (reference_job['job_id'],)).fetchone()[0])
    Path(payload['references'][0]['file']).unlink()
    assert worker.run_once() == reference_job['job_id']
    assert jobs.get(reference_job['job_id'])['status'] == 'FAILED'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (reference_job['job_id'],)).fetchone()[0] == 'released'
    assert called == []
