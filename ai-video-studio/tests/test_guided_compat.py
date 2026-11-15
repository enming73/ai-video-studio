import json

import pytest

from studio import attempts, core, guided_compat as compat, jobs, ledger, timeline
from studio import guided_generation as generation, guided_workflow as workflow


@pytest.fixture
def guided_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    pid = core.create('A river', 10, 'minimax', 5)
    workflow.create(pid)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A river flows.', 'duration': 10}]}, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    result = generation.submit(pid, 1, 2, confirm_paid=True, request_id='new')
    return jobs.get(result['id'])


def make_attempt(job):
    return attempts.create(job['id'], provider='minimax', model='MiniMax-H3',
                           input_data=json.loads(job['payload_json']))


def test_projection_matches_legacy_attempt_shape(guided_job):
    with core.connect() as db:
        legacy_keys = {r['name'] for r in db.execute('PRAGMA table_info(guided_attempts)')}
    result = compat.project_job(guided_job)
    assert set(result) == legacy_keys | {'created'}
    assert result['request_id'] == 'new'
    assert result['duration'] == 10
    attempt = make_attempt(guided_job)
    projected = compat.find(guided_job['project_id'], guided_job['id'])
    assert projected['id'] == attempt['id']
    assert compat.find(guided_job['project_id'], attempt['id']) == projected
    with pytest.raises(ValueError, match='Unknown attempt'):
        compat.find('another-project', attempt['id'])


@pytest.mark.parametrize('job_status,attempt_status,expected', [
    ('PENDING', None, 'QUEUED'), ('RUNNING', None, 'SUBMITTING'),
    ('RUNNING', 'PLANNED', 'QUEUED'), ('RUNNING', 'RESERVED', 'QUEUED'),
    ('RUNNING', 'SUBMITTING', 'SUBMITTING'),
    ('RUNNING', 'SUBMITTED', 'SUBMITTED'), ('RUNNING', 'POLLING', 'SUBMITTED'),
    ('RUNNING', 'DOWNLOADING', 'DOWNLOAD_PENDING'),
    ('RUNNING', 'VALIDATING', 'DOWNLOAD_PENDING'),
    ('RUNNING', 'COMPLETED', 'COMPLETED'), ('SUCCEEDED', 'COMPLETED', 'COMPLETED'),
    ('BLOCKED', 'UNKNOWN_REMOTE_STATE', 'SUBMITTING'),
    ('BLOCKED', 'VALIDATING', 'DOWNLOAD_PENDING'),
    ('FAILED', 'FAILED_FINAL', 'FAILED'), ('RUNNING', 'FAILED_RETRYABLE', 'FAILED'),
    ('CANCELLED', 'SUBMITTED', 'ABANDONED'), ('BLOCKED', 'ABANDONED', 'ABANDONED'),
])
def test_status_mapping_covers_job_states(guided_job, job_status, attempt_status, expected):
    if attempt_status:
        attempt = make_attempt(guided_job)
        with core.connect() as db:
            db.execute('UPDATE job_attempts SET status=? WHERE id=?', (attempt_status, attempt['id']))
    job = {**guided_job, 'status': job_status, 'block_reason': 'unknown_remote' if job_status == 'BLOCKED' else None}
    result = compat.project_job(job)
    assert result['status'] == expected
    if job_status == 'BLOCKED':
        assert 'unknown_remote' in result['error']


def test_unmappable_status_is_explicit(guided_job):
    for state in ('PAUSED', 'FUTURE_STATE'):
        with pytest.raises(ValueError, match='Unsupported Guided'):
            compat.project_job({**guided_job, 'status': state})


def test_attempts_merges_legacy_and_jobs(guided_job):
    pid = guided_job['project_id']
    shot_id = json.loads(guided_job['payload_json'])['shot_id']
    with core.connect() as db:
        db.execute('INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status) VALUES (?,?,?,?,?,?,?,?,?)',
                   ('a'*32, pid, shot_id, 'old', 'Old river', 10, 'minimax', 1, 'SUBMITTED'))
    # Ordinary CLI jobs are not Guided history.
    jobs.create(pid, kind='video', payload={'shot_id': shot_id}, request_key='cli',
                run_id=guided_job['run_id'])
    rows = compat.attempt_records(pid)
    assert [row['id'] for row in rows] == [guided_job['id'], 'a'*32]
    assert compat.find(pid, 'a'*32)['remote_id'] is None
    assert len({row['id'] for row in rows}) == 2


@pytest.mark.parametrize('billing,expected', [('reserved', 1.25), ('unknown', 1.25),
                                           ('settled', 0.75), ('released', 0)])
def test_projection_includes_cost_and_output(guided_job, billing, expected):
    attempt = make_attempt(guided_job)
    entry = attempts.prepare_submission(attempt['id'], amount_microusd=1_250_000,
                                        pricing_version='test:v1')
    if billing == 'settled':
        ledger.settle(entry, 750_000, 'provider bill')
    elif billing == 'released':
        ledger.release(entry, 'not charged')
    elif billing == 'unknown':
        ledger.mark_unknown(entry)
    pid = guided_job['project_id']
    target = core.HOME / pid / 'original.mp4'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'video')
    asset = timeline.import_asset(pid, 1, target, source='job:' + guided_job['id'])
    output = {'file': str(target), 'video_url': 'https://example.test/video'}
    with core.connect() as db:
        db.execute("UPDATE job_attempts SET remote_id='remote',output_json=? WHERE id=?",
                   (json.dumps(output), attempt['id']))
    result = compat.project_job({**guided_job, 'status': 'SUCCEEDED', 'output_json': json.dumps(output)})
    assert result['reserved_usd'] == expected
    assert result['remote_id'] == 'remote'
    assert result['video_url'] == 'https://example.test/video'
    assert result['file'] == str(target)
    assert result['asset_id'] == asset
