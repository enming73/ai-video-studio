import pytest
import shutil
import time
from fastapi.testclient import TestClient
from studio import core, webapp


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return TestClient(webapp.app)


def make_project(api):
    r = api.post('/api/production/projects', json={'prompt': 'Elephant in rainforest', 'duration': 20,
                                                  'provider': 'mock', 'budget': 2, 'guided': True})
    assert r.status_code == 201
    pid = r.json()['project_id']
    # Exercise pre-upgrade projects whose character stage was not opted in.
    with core.connect() as db:
        db.execute('DELETE FROM guided_character_settings WHERE project_id=?', (pid,))
    return pid


def test_guided_api_default_preview_and_paid_gate(api, monkeypatch):
    pid = make_project(api)
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    response = api.post(f'/api/projects/{pid}/guided/plan/preview', json={'model': 'test-model'})
    assert response.status_code == 200
    assert response.json()['estimated_cost_usd'] is None
    response = api.post(f'/api/projects/{pid}/guided/plan', json={'model': 'test-model', 'revision': 0})
    assert response.status_code == 400
    assert api.post(f'/api/projects/{pid}/production/plan', json={'shot_length': 10}).status_code == 400


def test_guided_api_draft_confirmation_and_render_gate(api):
    pid = make_project(api)
    draft = {'character_bible': {}, 'shots': [
        {'prompt': 'The elephant wanders in a rainforest.', 'duration': 8},
        {'prompt': 'The elephant approaches a clear river.', 'duration': 12}]}
    base = f'/api/projects/{pid}/guided'
    r = api.put(base + '/draft', json={'revision': 0, 'draft': draft})
    assert r.status_code == 200
    assert api.post(base + '/storyboard/confirm', json={'revision': 0}).status_code == 400
    assert api.post(base + '/storyboard/confirm', json={'revision': r.json()['revision']}).status_code == 200
    assert api.post(base + '/render', json={'revision': 2}).status_code == 400
    assert api.post(base + '/shots/2/preview', json={}).status_code == 400


def test_legacy_project_is_not_guided_by_default(api):
    response = api.post('/api/production/projects', json={'prompt': 'Existing workflow', 'duration': 20,
                                                          'provider': 'mock', 'budget': 0})
    pid = response.json()['project_id']
    assert api.get(f'/api/projects/{pid}/guided/').status_code == 400
    assert api.post(f'/api/projects/{pid}/production/plan', json={'shot_length': 10}).status_code == 200


def test_guided_shot_description_can_be_edited(api):
    pid = make_project(api)
    base = f'/api/projects/{pid}/guided'
    draft = {'character_bible': {}, 'shots': [{'prompt': 'Elephant walks through the forest.', 'duration': 20}]}
    r = api.put(base + '/draft', json={'revision': 0, 'draft': draft})
    r = api.post(base + '/storyboard/confirm', json={'revision': r.json()['revision']})
    r = api.patch(base + '/shots/1', json={'revision': r.json()['revision'],
                                           'prompt': 'Elephant slowly approaches the river.'})
    assert r.status_code == 200
    assert r.json()['shots'][0]['prompt'] == 'Elephant slowly approaches the river.'
    assert not r.json()['shots'][0]['confirmed']


def test_duplicate_planning_request_is_not_recharged(api, monkeypatch):
    pid = make_project(api)
    calls = []
    def llm(*args):
        calls.append(1)
        return {'character_bible': {}, 'shots': [{'prompt': 'An elephant walks through a forest.', 'duration': 20}]}
    monkeypatch.setattr(core, '_llm_json', llm)
    base = f'/api/projects/{pid}/guided'
    body = {'model': 'fake', 'revision': 0, 'budget_usd': 1, 'confirm_paid': True, 'request_id': 'unique'}
    first = api.post(base + '/plan', json=body)
    second = api.post(base + '/plan', json=body)
    assert first.status_code == second.status_code == 200
    assert len(calls) == 1
    assert first.json()['draft'] == second.json()['draft']


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg/ffprobe required')
def test_guided_mock_end_to_end_with_real_ffmpeg(api):
    response = api.post('/api/production/projects', json={'prompt': 'Two scenes', 'duration': 2,
                                                          'provider': 'mock', 'budget': 0, 'guided': True})
    pid = response.json()['project_id']
    with core.connect() as db:
        db.execute('DELETE FROM guided_character_settings WHERE project_id=?', (pid,))
    base = f'/api/projects/{pid}/guided'
    draft = {'character_bible': {}, 'shots': [
        {'prompt': 'A small elephant walks in the forest.', 'duration': 1},
        {'prompt': 'The elephant approaches a river.', 'duration': 1}]}
    state = api.put(base + '/draft', json={'draft': draft, 'revision': 0}).json()
    state = api.post(base + '/storyboard/confirm', json={'revision': state['revision']}).json()
    for seq in (1, 2):
        job = api.post(base + f'/shots/{seq}/generate', json={'request_id': f'shot-{seq}'}).json()
        assert job['status'] == 'COMPLETED', job
        assert api.put(f'/api/projects/{pid}/shots/{seq}/selection', json={'asset_id': job['asset_id']}).status_code == 200
        state = api.get(base + '/').json()
        result = api.post(base + f'/shots/{seq}/confirm', json={'asset_id': job['asset_id'], 'revision': state['revision']})
        assert result.status_code == 200, result.text
        state = result.json()
    result = api.post(base + '/render', json={'revision': state['revision']})
    assert result.status_code == 202, result.text
    job_id = result.json()['id']
    for _ in range(100):
        job = api.get(f'/api/projects/{pid}/renders/{job_id}').json()
        if job['status'] in ('completed', 'failed'):
            break
        time.sleep(0.1)
    assert job['status'] == 'completed', job.get('error')
    assert api.get(f'/api/projects/{pid}/renders/{job_id}/video').status_code == 200


def test_new_guided_project_exposes_character_stage_and_roles(api):
    response = api.post('/api/production/projects', json={'prompt': 'Rainforest story', 'duration': 20,
                                                          'provider': 'mock', 'budget': 0, 'guided': True})
    pid = response.json()['project_id']
    base = f'/api/projects/{pid}/guided'
    assert api.get(base + '/').json()['character_stage_enabled'] is True
    result = api.post(base + '/characters', json={'name': 'Luma', 'description': 'A gray elephant', 'revision': 0})
    assert result.status_code == 200, result.text
    assert result.json()['characters'][0]['name'] == 'Luma'
