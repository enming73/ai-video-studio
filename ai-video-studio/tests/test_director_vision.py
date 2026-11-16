import json

import httpx
import pytest

from studio import core, director_vision as v
from studio import vision_jobs


def test_normalize():
    assert v.normalize({'recommendation':'manual_review'})['action_match'] is None
    with pytest.raises(ValueError): v.normalize({'recommendation':'keep','visual_quality':6})
    with pytest.raises(ValueError): v.normalize({'recommendation':'delete'})

def test_budget_plan(monkeypatch):
    monkeypatch.setattr(v.director,'report',lambda pid,rid:{'shots':[
        {'seq':1,'frames':[{'file':'a.jpg'}],'status':'technical_pass'},
        {'seq':2,'frames':[],'status':'missing'}]})
    _,shots,estimate=v.plan('p','r',0.2,0.1)
    assert len(shots)==1 and not estimate['within_budget']
    _,shots,estimate=v.plan('p','r',0.2,0.2,{2})
    assert not shots and estimate['within_budget']


def _shot_payload(tmp_path, monkeypatch, *, frame_in_root=True):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = 'p123'
    report_id = 'a1b2c3d4e5f6'
    root = tmp_path / pid / 'director-reports' / report_id
    root.mkdir(parents=True, exist_ok=True)
    if frame_in_root:
        frame = root / 'frame-01.jpg'
    else:
        frame = tmp_path / 'outside.jpg'
    frame.write_bytes(b'\xff\xd8' + b'x' * 32)
    return {'kind': 'director', 'project_id': pid, 'report_id': report_id,
            'review_id': 'c' * 12, 'seq': 1, 'prompt': 'p',
            'frames': [str(frame)], 'source_asset_id': 'a', 'model': 'm',
            'base_url': 'https://api.example.com/v1', 'budget_microusd': 200000}


def test_execute_shot_pre_request_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    payload = _shot_payload(tmp_path, monkeypatch, frame_in_root=False)
    with pytest.raises(vision_jobs.PreRequestFailure):
        v.execute_shot(payload)


def test_execute_shot_frame_oserror_is_pre_request_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    payload = _shot_payload(tmp_path, monkeypatch)
    monkeypatch.setattr(v, '_image_payload',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('unreadable')))
    with pytest.raises(vision_jobs.PreRequestFailure):
        v.execute_shot(payload)


def test_execute_shot_unknown_remote(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    payload = _shot_payload(tmp_path, monkeypatch)
    monkeypatch.setattr(v, '_post',
                        lambda *a, **k: (_ for _ in ()).throw(httpx.HTTPError('down')))
    with pytest.raises(vision_jobs.UnknownRemote):
        v.execute_shot(payload)


def _status_client(status):
    def client_factory(*args, **kwargs):
        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                request = httpx.Request('POST', 'https://api.example.com/v1')
                return httpx.Response(status, request=request)
        return _Client()
    return client_factory


def test_post_classifies_http_status_errors(monkeypatch):
    for status, exc_type in ((401, vision_jobs.AuthFailure),
                             (403, vision_jobs.AuthFailure),
                             (429, vision_jobs.RateLimited),
                             (400, vision_jobs.PreRequestFailure),
                             (404, vision_jobs.PreRequestFailure)):
        monkeypatch.setattr(v.httpx, 'Client', _status_client(status))
        with pytest.raises(exc_type):
            v._post('https://api.example.com/v1', 'k', 'm', [])
    # 5xx stays ambiguous and is re-raised for the executor's unknown handling.
    monkeypatch.setattr(v.httpx, 'Client', _status_client(500))
    with pytest.raises(httpx.HTTPStatusError):
        v._post('https://api.example.com/v1', 'k', 'm', [])


def test_execute_shot_post_response_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    payload = _shot_payload(tmp_path, monkeypatch)
    monkeypatch.setattr(v, '_post',
                        lambda *a, **k: {'choices': [{'message': {'content': 'not json'}}]})
    with pytest.raises(vision_jobs.PostResponseFailure):
        v.execute_shot(payload)


def test_execute_shot_returns_reviewed(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    payload = _shot_payload(tmp_path, monkeypatch)
    good = json.dumps({'character_consistency': 5, 'scene_match': 4,
                       'action_match': None, 'visual_quality': 3,
                       'findings': ['ok'], 'uncertainty': 'low',
                       'recommendation': 'keep'})
    monkeypatch.setattr(v, '_post', lambda *a, **k: {
        'choices': [{'message': {'content': good}}],
        'usage': {'prompt_tokens': 1}})
    result = v.execute_shot(payload)
    assert result['status'] == 'reviewed'
    assert result['seq'] == 1
    assert result['source_asset_id'] == 'a'


def test_assemble_marks_partial_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('x', 5, 'mock', 5)
    report_id = 'a1b2c3d4e5f6'
    review_id = 'd' * 12
    run_id = 'r' * 32
    estimate = {'shots': 2, 'reserved_ceiling_usd': 0.4, 'budget_usd': 1,
                'within_budget': True}
    job1 = {'id': 'j1', 'status': 'SUCCEEDED',
            'payload_json': json.dumps({'seq': 1}),
            'output_json': json.dumps({'seq': 1, 'status': 'reviewed',
                                       'review': {'recommendation': 'keep'},
                                       'usage': {}, 'source_asset_id': 'a'})}
    job2 = {'id': 'j2', 'status': 'BLOCKED',
            'payload_json': json.dumps({'seq': 2}),
            'output_json': None, 'block_reason': 'unknown_remote',
            'error_detail': None, 'error_class': None}
    result = v.assemble(pid, [job2, job1], report_id=report_id, model='m',
                        base_url='https://x/v1', estimate=estimate,
                        review_id=review_id, run_id=run_id, request_id='v1')
    assert result['paid_api_calls'] == 2
    assert result['partial'] is True
    assert result['results'][0]['seq'] == 1
    assert result['results'][1]['status'] == 'error'
    assert result['job_ids'] == ['j1', 'j2']
    saved = json.loads((core.HOME / pid / 'director-reviews' / review_id /
                        'review.json').read_text(encoding='utf-8'))
    assert saved['review_id'] == review_id
    assert saved['paid_api_calls'] == 2
