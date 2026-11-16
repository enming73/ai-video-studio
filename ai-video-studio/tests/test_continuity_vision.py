import json

import pytest

from studio import continuity_vision as cv
from studio import core, vision_jobs

PAIR_RESULT = {
    'from_seq': 2, 'to_seq': 3, 'status': 'reviewed',
    'review': {'severity': 'none', 'confidence': 0.9, 'observations': [],
               'uncertainties': [], 'recommendation': 'keep',
               'reason': 'continuous', 'proposed_prompt': None},
    'usage': {'prompt_tokens': 10, 'completion_tokens': 20},
    'source_asset_ids': ['asset-2', 'asset-3'],
}


@pytest.fixture(autouse=True)
def handlers():
    from studio.handlers import register_all
    register_all()


@pytest.fixture
def continuity_report(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A little elephant in the forest', 10, 'mock', 10)
    report_id = 'b2c3d4e5f6a7'
    root = tmp_path / pid / 'continuity-reports' / report_id
    root.mkdir(parents=True, exist_ok=True)
    boundaries = []
    for from_seq, to_seq in ((2, 3), (3, 4)):
        folder = root / f'boundary-{from_seq:03d}-{to_seq:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        frames = {'out': [], 'in': []}
        for side in ('out', 'in'):
            for i in (1, 2):
                frame = folder / f'{side}-{i}.jpg'
                frame.write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 64)
                frames[side].append({'time': 0.0, 'file': str(frame)})
        boundaries.append({
            'from_seq': from_seq, 'to_seq': to_seq,
            'from_prompt': f'Shot {from_seq} prompt',
            'to_prompt': f'Shot {to_seq} prompt',
            'from_asset_id': f'asset-{from_seq}',
            'to_asset_id': f'asset-{to_seq}',
            'status': 'ready', 'warnings': [], 'frames': frames,
        })
    report = {'schema_version': 1, 'report_id': report_id, 'project_id': pid,
              'created': core.now(), 'mode': 'offline_boundary_evidence',
              'paid_api_calls': 0, 'estimated_api_cost_usd': 0, 'disclaimer': '',
              'boundaries': boundaries}
    (root / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return pid, report_id


def test_normalize():
    good = {'severity': 'medium', 'confidence': 0.8,
            'recommendation': 'revise_next_opening',
            'observations': ['new frog'], 'uncertainties': ['only sampled frames'],
            'reason': 'abrupt', 'proposed_prompt': 'frog emerges from leaves'}
    assert cv.normalize(good)['proposed_prompt'].startswith('frog')
    with pytest.raises(ValueError):
        cv.normalize({**good, 'confidence': 2})
    with pytest.raises(ValueError):
        cv.normalize({**good, 'recommendation': 'pay_now'})


def test_dry_run_no_calls(monkeypatch):
    monkeypatch.setattr(cv.continuity, 'report', lambda pid, rid: {'boundaries': [
        {'from_seq': 2, 'to_seq': 3, 'status': 'ready',
         'frames': {'out': [{}, {}], 'in': [{}, {}]}}
    ]})
    r = cv.review('p', 'abcdef123456', 'model', pairs=[2], budget_usd=0)
    assert r['paid_api_calls'] == 0 and r['pairs'][0]['to_seq'] == 3
    assert not r['within_budget']
    with pytest.raises(ValueError, match='Budget insufficient'):
        cv.review('p', 'abcdef123456', 'model', live=True, budget_usd=0,
                  api_key='fake')


def test_frame_path_restriction(tmp_path):
    root = tmp_path / 'safe'
    root.mkdir()
    outside = tmp_path / 'secret.jpg'
    outside.write_bytes(b'secret')
    with pytest.raises(ValueError):
        cv._frame({'file': str(outside)}, root)


def test_continuity_review_creates_one_job_per_ready_pair(continuity_report, monkeypatch):
    pid, report_id = continuity_report
    monkeypatch.setattr(cv, 'execute_pair', lambda payload: PAIR_RESULT)
    result = cv.review(pid, report_id, 'model', live=True,
                       budget_usd=1, per_pair_ceiling_usd=0.2,
                       pairs=[2], request_id='c1')
    assert len(result['job_ids']) == 1
    assert result['review_id']


def test_continuity_review_replay_returns_same_review_and_jobs(continuity_report, monkeypatch):
    pid, report_id = continuity_report
    monkeypatch.setattr(cv, 'execute_pair', lambda payload: PAIR_RESULT)
    first = cv.review(pid, report_id, 'model', live=True,
                      budget_usd=1, per_pair_ceiling_usd=0.2,
                      pairs=[2], request_id='c1')
    second = cv.review(pid, report_id, 'model', live=True,
                       budget_usd=1, per_pair_ceiling_usd=0.2,
                       pairs=[2], request_id='c1')
    assert second['review_id'] == first['review_id']
    assert second['job_ids'] == first['job_ids']


def test_continuity_review_marks_partial_failure(continuity_report, monkeypatch):
    pid, report_id = continuity_report
    monkeypatch.setattr(
        cv, 'execute_pair',
        lambda payload: (_ for _ in ()).throw(vision_jobs.UnknownRemote('timeout'))
        if payload['from_seq'] == 3 else PAIR_RESULT)
    result = cv.review(pid, report_id, 'model', live=True,
                       budget_usd=1, per_pair_ceiling_usd=0.2,
                       request_id='c2')
    assert result['results'][1]['status'] == 'error'
    assert result['paid_api_calls'] == 2
