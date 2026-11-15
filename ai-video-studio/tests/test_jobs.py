"""Jobs are persisted, idempotent and never report a running task as paused."""
import json

import pytest

from studio import core, jobs, ledger


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Job project', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    return pid, run_id


def make(project, key='req-1', payload=None, **kwargs):
    pid, run_id = project
    return jobs.create(pid, kind='video', payload=payload or {'prompt': 'p'},
                       request_key=key, run_id=run_id, **kwargs)


def test_create_is_idempotent_and_replays_same_job(project):
    first = make(project)
    second = make(project)
    assert first['id'] == second['id']
    assert jobs.list_for_project(project[0]) == [first]


def test_same_key_different_payload_is_rejected(project):
    make(project, payload={'prompt': 'one'})
    with pytest.raises(ValueError, match='payload'):
        make(project, payload={'prompt': 'two'})


def test_create_requires_open_run_from_same_project(project):
    pid, run_id = project
    other = core.create('Other', 10, 'mock', 5)
    other_run = ledger.open_run(other, 1_000_000)
    with pytest.raises(ValueError, match='run'):
        jobs.create(pid, kind='video', payload={}, request_key='x', run_id=None)
    with pytest.raises(ValueError, match='project'):
        jobs.create(pid, kind='video', payload={}, request_key='x', run_id=other_run)
    ledger.close_run(run_id)
    with pytest.raises(ValueError, match='open'):
        jobs.create(pid, kind='video', payload={}, request_key='y', run_id=run_id)


def test_pause_resume_cancel_transitions(project):
    job = make(project)
    assert jobs.pause(job['id'])['status'] == 'PAUSED'
    assert jobs.resume(job['id'])['status'] == 'PENDING'
    assert jobs.cancel(job['id'])['status'] == 'CANCELLED'

    running = make(project, key='req-2')
    jobs.set_status(running['id'], 'RUNNING')
    paused = jobs.pause(running['id'])
    assert paused['status'] == 'RUNNING'
    assert paused['pause_requested'] == 1
    assert jobs.stop_requested(running['id']) == 'pause'

    cancelled = jobs.cancel(running['id'])
    assert cancelled['status'] == 'RUNNING'
    assert cancelled['cancel_requested'] == 1
    assert jobs.stop_requested(running['id']) == 'cancel'
    with core.connect() as db:
        events = [dict(row) for row in db.execute(
            'SELECT type,data_json FROM job_events WHERE job_id=? ORDER BY created',
            (running['id'],))]
    assert any('remote may continue' in row['data_json'] for row in events)


def test_list_filters_by_status_and_project(project):
    one = make(project, key='a')
    two = make(project, key='b')
    jobs.cancel(two['id'])
    assert [job['id'] for job in jobs.list_for_project(project[0], status='PENDING')] == [one['id']]
    assert jobs.list_for_project('other') == []


def test_set_status_records_output_and_block_reason(project):
    job = make(project)
    jobs.set_status(job['id'], 'RUNNING')
    output = {'file': '/tmp/x.mp4'}
    done = jobs.set_status(job['id'], 'SUCCEEDED', output=output)
    assert done['status'] == 'SUCCEEDED'
    assert json.loads(done['output_json']) == output
    blocked = make(project, key='blocked')
    jobs.set_status(blocked['id'], 'RUNNING')
    result = jobs.set_status(blocked['id'], 'BLOCKED', block_reason='unknown_remote')
    assert result['block_reason'] == 'unknown_remote'
    with pytest.raises(ValueError):
        jobs.set_status(done['id'], 'PENDING')
