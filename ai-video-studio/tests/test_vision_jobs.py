"""Director vision review jobs: per-shot enqueue, budget gate, partial failure.

These tests never call a real vision API. They monkeypatch
``director_vision.execute_shot`` to a canned result, or inject an
``UnknownRemote`` fault so the request never leaves the process.
"""
import json

import pytest

from studio import core, director_vision, jobs, vision_jobs

REVIEW_RESULT = {
    'seq': 1,
    'status': 'reviewed',
    'review': {'character_consistency': 5, 'scene_match': 4,
               'action_match': None, 'visual_quality': 3,
               'findings': ['ok'], 'uncertainty': 'low',
               'recommendation': 'keep'},
    'usage': {'prompt_tokens': 10, 'completion_tokens': 20},
    'source_asset_id': 'asset-1',
}


@pytest.fixture(autouse=True)
def handlers():
    from studio.handlers import register_all
    register_all()


@pytest.fixture
def director_report(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A little elephant in the forest', 10, 'mock', 10)
    core.plan(pid, 5)
    report_id = 'a1b2c3d4e5f6'
    root = tmp_path / pid / 'director-reports' / report_id
    shots = []
    for seq in (1, 2):
        folder = root / f'shot-{seq:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        frame = folder / 'frame-01.jpg'
        frame.write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 64)
        shots.append({
            'seq': seq, 'shot_id': f'{pid}-s{seq:03d}',
            'prompt': f'Shot {seq} prompt', 'planned_duration': 5,
            'asset_id': f'asset-{seq}', 'checks': {}, 'warnings': [],
            'frames': [{'time': 1.0, 'file': str(frame)}],
            'status': 'technical_pass',
        })
    report = {'schema_version': 1, 'report_id': report_id, 'project_id': pid,
              'created': core.now(), 'mode': 'offline_technical_only',
              'paid_api_calls': 0, 'estimated_api_cost_usd': 0, 'disclaimer': '',
              'summary': {'total': 2, 'technical_pass': 2, 'needs_review': 0},
              'shots': shots}
    root.mkdir(parents=True, exist_ok=True)
    (root / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return pid, report_id


def test_director_review_creates_one_job_per_shot(director_report, monkeypatch):
    pid, report_id = director_report
    monkeypatch.setattr(
        director_vision, 'execute_shot',
        lambda payload: {**REVIEW_RESULT, 'seq': payload['seq'],
                         'source_asset_id': payload['source_asset_id']})
    result = director_vision.review(pid, report_id, 'model', live=True,
                                    budget_usd=1, per_shot_ceiling_usd=0.2,
                                    request_id='v1')
    assert len(result['job_ids']) == 2
    assert result['review_id']
    assert all(jobs.get(job_id)['kind'] == 'vision_review'
               for job_id in result['job_ids'])


def test_no_jobs_when_total_ceiling_exceeds_budget(director_report):
    pid, report_id = director_report
    with pytest.raises(ValueError, match='Budget insufficient'):
        director_vision.review(pid, report_id, 'model', live=True,
                               budget_usd=0.1, per_shot_ceiling_usd=0.2,
                               request_id='v1')
    with core.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='vision_review'"
                          ).fetchone()[0] == 0


def test_director_review_marks_partial_failure(director_report, monkeypatch):
    pid, report_id = director_report
    monkeypatch.setattr(
        director_vision, 'execute_shot',
        lambda payload: (_ for _ in ()).throw(vision_jobs.UnknownRemote('timeout'))
        if payload['seq'] == 2
        else {**REVIEW_RESULT, 'seq': payload['seq'],
              'source_asset_id': payload['source_asset_id']})
    result = director_vision.review(pid, report_id, 'model', live=True,
                                    budget_usd=1, per_shot_ceiling_usd=0.2,
                                    request_id='v1')
    assert result['results'][1]['status'] == 'error'
    assert result['paid_api_calls'] == 2
    with core.connect() as db:
        assert db.execute(
            "SELECT status FROM jobs WHERE request_key LIKE 'vision_review:%' "
            'ORDER BY created').fetchall()


def test_vision_pre_request_releases_reservation(director_report, monkeypatch):
    from studio import worker
    pid, report_id = director_report
    monkeypatch.setattr(
        director_vision, 'execute_shot',
        lambda payload: (_ for _ in ()).throw(
            vision_jobs.PreRequestFailure('missing key')))
    queued = vision_jobs.enqueue_director(
        pid, report_id, 'model', None, 1, 0.2, None, request_id='w1')
    worker.run_once()
    job = jobs.get(queued['job_ids'][0])
    assert job['status'] == 'FAILED'
    with core.connect() as db:
        row = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                         (queued['job_ids'][0],)).fetchone()
        assert row is not None and row[0] == 'released'


def test_vision_unknown_remote_blocks_and_retains(director_report, monkeypatch):
    from studio import worker
    pid, report_id = director_report
    monkeypatch.setattr(
        director_vision, 'execute_shot',
        lambda payload: (_ for _ in ()).throw(vision_jobs.UnknownRemote('timeout')))
    queued = vision_jobs.enqueue_director(
        pid, report_id, 'model', None, 1, 0.2, None, request_id='w2')
    worker.run_once()
    job = jobs.get(queued['job_ids'][0])
    assert job['status'] == 'BLOCKED'
    assert job['block_reason'] == 'unknown_remote'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (queued['job_ids'][0],)).fetchone()[0] == 'reserved'


def test_distinct_request_ids_over_same_content_get_independent_runs(
        director_report, monkeypatch):
    pid, report_id = director_report
    monkeypatch.setattr(
        director_vision, 'execute_shot',
        lambda payload: {**REVIEW_RESULT, 'seq': payload['seq'],
                         'source_asset_id': payload['source_asset_id']})
    first = director_vision.review(pid, report_id, 'model', live=True,
                                   budget_usd=1, per_shot_ceiling_usd=0.2,
                                   request_id='a')
    second = director_vision.review(pid, report_id, 'model', live=True,
                                    budget_usd=1, per_shot_ceiling_usd=0.2,
                                    request_id='b')
    assert first['run_id'] != second['run_id']
    assert first['review_id'] != second['review_id']
    assert set(first['job_ids']) != set(second['job_ids'])
    # Both batches reserve and execute independently: no budget_exceeded block.
    assert first['partial'] is False
    assert second['partial'] is False
    all_ids = list(first['job_ids']) + list(second['job_ids'])
    assert all(jobs.get(jid)['status'] == 'SUCCEEDED' for jid in all_ids)


def test_frame_read_oserror_releases_reservation(director_report, monkeypatch):
    from studio import worker
    pid, report_id = director_report
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    posted = []
    monkeypatch.setattr(director_vision, '_image_payload',
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError('frame unreadable')))
    monkeypatch.setattr(director_vision, '_post',
                        lambda *a, **k: posted.append('post') or {'choices': []})
    queued = vision_jobs.enqueue_director(
        pid, report_id, 'model', None, 1, 0.2, None, request_id='w3')
    worker.run_once()
    job = jobs.get(queued['job_ids'][0])
    assert job['status'] == 'FAILED'
    assert posted == []
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (queued['job_ids'][0],)).fetchone()[0] == 'released'
