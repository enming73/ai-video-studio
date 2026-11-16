import pytest
import shutil
import time
from fastapi.testclient import TestClient
from studio import core, webapp
from studio import guided_boundaries as boundaries, guided_workflow as workflow
from tests.helpers import add_version


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
    preview = api.post(base + '/shots/2/preview', json={})
    assert preview.status_code == 200
    assert preview.json()['provider'] == 'mock'
    assert preview.json()['model'] == 'mock'
    assert preview.json()['first_frame_id'] is None


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
    monkeypatch.setattr(core, '_llm_json', lambda *args: calls.append(1) or pytest.fail('unexpected paid call'))
    base = f'/api/projects/{pid}/guided'
    body = {'model': 'fake', 'revision': 0, 'budget_usd': 1, 'confirm_paid': True, 'request_id': 'unique'}
    first = api.post(base + '/plan', json=body)
    second = api.post(base + '/plan', json=body)
    assert first.status_code == second.status_code == 200
    assert len(calls) == 0
    assert first.json()['status'] == 'QUEUED'
    assert first.json() == second.json()
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_planning_attempts').fetchone()[0] == 0


def test_plan_resolve_requires_confirmation_and_acknowledges(api, monkeypatch):
    import json
    from studio import attempts, jobs, request_jobs
    pid = make_project(api)
    base = f'/api/projects/{pid}/guided'
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    body = {'model': 'fake', 'revision': 0, 'budget_usd': 1, 'confirm_paid': True, 'request_id': 'plan-1'}
    queued = api.post(base + '/plan', json=body).json()
    assert queued['status'] == 'QUEUED'
    job = request_jobs.find(pid, kind='llm_storyboard', request_id='plan-1')
    attempt = attempts.create(job['id'], provider='llm', model='fake',
                              input_data=json.loads(job['payload_json']))
    attempts.prepare_submission(attempt['id'], amount_microusd=1_000_000, pricing_version='test:v1')
    attempts.mark_unknown(attempt['id'], 'uncertain submission')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='BLOCKED',block_reason='unknown_remote' WHERE id=?",
                   (job['id'],))
    listed = api.get(base + '/plan/attempts').json()
    assert any(r['request_id'] == 'plan-1' and r['status'] == 'UNKNOWN' for r in listed)
    rejected = api.post(base + '/plan/attempts/plan-1/resolve', json={})
    assert rejected.status_code == 400
    assert 'confirmation' in rejected.json()['detail']
    resolved = api.post(base + '/plan/attempts/plan-1/resolve', json={'confirm_checked': True})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()['status'] == 'ACKNOWLEDGED'
    with core.connect() as db:
        assert jobs.get(job['id'], db=db)['status'] == 'CANCELLED'
        assert db.execute('SELECT billing_status FROM cost_entries WHERE attempt_id=?',
                          (attempt['id'],)).fetchone()[0] == 'reserved'


def test_plan_resolve_legacy_row_requires_confirmation(api):
    pid = make_project(api)
    base = f'/api/projects/{pid}/guided'
    with core.connect() as db:
        db.execute('INSERT INTO guided_planning_attempts VALUES (?,?,?,?,?,?,?)',
                   (pid, 'legacy-plan', 'UNKNOWN', 'm', 1, None, 'failure'))
    rejected = api.post(base + '/plan/attempts/legacy-plan/resolve', json={})
    assert rejected.status_code == 400
    resolved = api.post(base + '/plan/attempts/legacy-plan/resolve', json={'confirm_checked': True})
    assert resolved.status_code == 200
    assert resolved.json()['status'] == 'ACKNOWLEDGED'


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


def make_boundary_project(api):
    response = api.post('/api/production/projects', json={
        'prompt': 'Elephant in forest', 'duration': 10, 'provider': 'mock', 'budget': 0, 'guided': True})
    pid = response.json()['project_id']
    base = f'/api/projects/{pid}/guided'
    response = api.post(base + '/characters/empty-confirm', json={'revision': 0})
    response = api.put(base + '/draft', json={'revision': response.json()['revision'], 'draft': {
        'character_bible': {}, 'shots': [{'prompt': 'An elephant walks.', 'duration': 10, 'character_ids': []}]}})
    response = api.post(base + '/storyboard/confirm', json={'revision': response.json()['revision']})
    return pid, response.json(), base


def test_boundary_routes(api):
    pid, state, base = make_boundary_project(api)
    list_response = api.get(base + '/boundaries')
    assert list_response.status_code == 200
    assert [b['position'] for b in list_response.json()] == [0, 1]
    for position in (0, 1):
        response = api.post(base + f"/boundaries/{state['boundaries'][position]['id']}/frames/preview", json={})
        assert response.status_code == 200, response.text
    assert api.get(base + '/shots/1/frames').status_code == 404


def test_structure_route_rejects_invalid_total(api):
    pid, state, base = make_boundary_project(api)
    body = {'revision': state['revision'], 'shots': [
        {'id': state['shots'][0]['id'], 'prompt': 'An elephant walks.', 'duration': 9, 'character_ids': []}]}
    assert api.put(base + '/structure', json=body).status_code == 400
    assert workflow.get(pid)['shots'][0]['duration'] == 10


def test_unconfirm_route(api):
    pid, state, base = make_boundary_project(api)
    boundary = state['boundaries'][0]['id']
    version = add_version(pid, boundary, 'b0', size='1536x864')
    boundaries.confirm(pid, boundary, version, workflow.get(pid)['revision'])
    response = api.request('DELETE', base + f'/boundaries/{boundary}/confirm',
                           json={'revision': workflow.get(pid)['revision']})
    assert response.status_code == 200
    assert workflow.get(pid)['boundaries'][0]['confirmed'] is False


def test_structure_route_creates_shot_without_id(api):
    pid, state, base = make_boundary_project(api)
    old_id = state['shots'][0]['id']
    body = {'revision': state['revision'], 'shots': [
        {'prompt': 'An elephant returns.', 'duration': 10, 'character_ids': []}]}
    response = api.put(base + '/structure', json=body)
    assert response.status_code == 200, response.text
    assert len(response.json()['shots']) == 1
    assert response.json()['shots'][0]['id'] != old_id


def test_structure_route_rejects_unhashable_id(api):
    pid, state, base = make_boundary_project(api)
    body = {'revision': state['revision'], 'shots': [
        {'id': [], 'prompt': 'An elephant walks.', 'duration': 10, 'character_ids': []}]}
    assert api.put(base + '/structure', json=body).status_code == 422


def test_structure_route_rejects_non_integer_duration(api):
    pid, state, base = make_boundary_project(api)
    body = {'revision': state['revision'], 'shots': [
        {'id': state['shots'][0]['id'], 'prompt': 'An elephant walks.', 'duration': 'ten',
         'character_ids': []}]}
    assert api.put(base + '/structure', json=body).status_code == 422


def test_structure_route_rejects_numeric_string_duration(api):
    pid, state, base = make_boundary_project(api)
    body = {'revision': state['revision'], 'shots': [
        {'id': state['shots'][0]['id'], 'prompt': 'An elephant walks.', 'duration': '10',
         'character_ids': []}]}
    assert api.put(base + '/structure', json=body).status_code == 422


def test_structure_route_requires_character_ids(api):
    pid, state, base = make_boundary_project(api)
    before = workflow.get(pid)
    body = {'revision': state['revision'], 'shots': [
        {'id': state['shots'][0]['id'], 'prompt': 'An elephant walks.', 'duration': 10}]}
    assert api.put(base + '/structure', json=body).status_code == 422
    assert workflow.get(pid) == before


def test_attempt_api_merges_jobs_and_reconciles_output(api, monkeypatch):
    import json
    from studio import guided_generation as generation, jobs, attempts
    pid = make_project(api)
    base = f'/api/projects/{pid}/guided'
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A river flows.', 'duration': 10},
        {'prompt': 'Leaves fall.', 'duration': 10}]}, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    first = api.post(base + '/shots/1/generate', json={
        'request_id': 'api-new', 'budget_usd': 2, 'confirm_paid': True}).json()
    assert first['status'] == 'QUEUED'
    assert api.get(base + '/attempts').json()[0] == first
    job = jobs.get(first['id'])
    attempt = attempts.create(job['id'], provider='minimax', model='MiniMax-H3',
                              input_data=json.loads(job['payload_json']))
    attempts.prepare_submission(attempt['id'], amount_microusd=1_000_000, pricing_version='test:v1')
    attempts.mark_unknown(attempt['id'], 'uncertain submission')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='BLOCKED',block_reason='unknown_remote' WHERE id=?", (job['id'],))
    for action in ('recover', 'trim', 'abandon'):
        rejected = api.post(base + '/attempts/' + attempt['id'] + '/' + action, json={})
        assert rejected.status_code == 400
        assert 'confirmation' in rejected.json()['detail']
    # Explicitly confirmed recovery queries once, and never submits.
    from studio.handlers import video
    from video_provider_core import RemoteStatus
    class Provider:
        def __init__(self, client): pass
        def submit_video(self, *args):
            pytest.fail('Recovery must not submit')
        def status(self, remote, *, model=None):
            return RemoteStatus('running')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    recovered = api.post(base + '/attempts/' + attempt['id'] + '/recover',
                         json={'remote_id': 'known', 'confirm_checked': True})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['status'] == 'SUBMITTED'
    target = core.HOME / pid / 'worker-result.mp4'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'generated')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='SUCCEEDED',output_json=? WHERE id=?",
                   (json.dumps({'file': str(target)}), job['id']))
        db.execute("UPDATE job_attempts SET status='COMPLETED' WHERE id=?", (attempt['id'],))
    result = api.get(base + '/attempts/' + first['id'])
    assert result.status_code == 200
    assert result.json()['id'] == attempt['id']
    assert result.json()['asset_id']
    assert result.json()['status'] == 'COMPLETED'
    assert api.get(base + '/attempts/' + attempt['id']).json() == result.json()
    assert generation.attempt(pid, first['id']) == result.json()
