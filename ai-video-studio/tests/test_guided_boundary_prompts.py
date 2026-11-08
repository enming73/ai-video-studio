import pytest
from fastapi.testclient import TestClient

from studio import core, guided_boundaries as boundaries, guided_workflow as workflow, webapp


@pytest.fixture(autouse=True)
def llm_env(monkeypatch):
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setenv('STUDIO_LLM_BASE_URL', 'https://example.com/v1')


@pytest.fixture
def llm(monkeypatch):
    seen = []

    def fake(system, user, model, base_url):
        seen.append({'system': system, 'user': user, 'model': model, 'base_url': base_url})
        return {'prompt': 'Wide shot of the elephant standing by the river at sunset, warm golden light.'}

    monkeypatch.setattr(core, '_llm_json', fake)
    return seen


def test_generate_prompt_stores_and_uses_llm_text(boundary_project, llm):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    result = boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                        confirm_paid=True, request_id='prompt-1',
                                        revision=state['revision'])
    assert result['status'] == 'COMPLETED'
    assert result['prompt'].startswith('Wide shot of the elephant')
    assert llm[0]['model'] == 'gpt-4o-mini'
    assert 'An elephant walks through the forest.' in llm[0]['user']
    info = boundaries.prompt_state(pid, boundary_id)
    assert info['effective_source'] == 'llm'
    assert info['llm_prompt_current'] is True
    preview = boundaries.preview(pid, boundary_id, 'gpt-image-2.5-sunburst')
    assert preview['prompt_source'] == 'llm'
    assert preview['prompt'].startswith(boundaries._SINGLE_FRAME)
    assert 'Wide shot of the elephant' in preview['prompt']


def test_generate_prompt_requires_budget_and_confirmation(boundary_project, monkeypatch):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('paid call must not happen'))
    with pytest.raises(ValueError):
        boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0,
                                   confirm_paid=True, request_id='p1', revision=state['revision'])
    with pytest.raises(ValueError):
        boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                   confirm_paid=False, request_id='p2', revision=state['revision'])


def test_llm_prompt_becomes_stale_after_shot_edit(boundary_project, llm):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                               confirm_paid=True, request_id='prompt-1', revision=state['revision'])
    workflow.edit_shot(pid, 1, 'A different elephant scene by the sea.',
                       workflow.get(pid)['revision'])
    info = boundaries.prompt_state(pid, boundary_id)
    assert info['llm_prompt_current'] is False
    assert info['effective_source'] == 'auto'
    assert 'Wide shot of the elephant' not in info['effective_prompt']


def test_failed_prompt_call_is_unresolved_and_blocks_regeneration(boundary_project, monkeypatch):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    monkeypatch.setattr(core, '_llm_json', lambda *args: (_ for _ in ()).throw(RuntimeError('network')))
    result = boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                        confirm_paid=True, request_id='prompt-1',
                                        revision=state['revision'])
    assert result['status'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='unresolved'):
        boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                   confirm_paid=True, request_id='prompt-2',
                                   revision=workflow.get(pid)['revision'])
    boundaries.resolve_prompt_attempt(pid, result['id'], confirm_checked=True)
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'prompt': 'A calm riverside still with the elephant.'})
    retry = boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                       confirm_paid=True, request_id='prompt-3',
                                       revision=workflow.get(pid)['revision'])
    assert retry['status'] == 'COMPLETED'


def test_clear_prompt_restores_auto(boundary_project, llm):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                               confirm_paid=True, request_id='prompt-1', revision=state['revision'])
    boundaries.clear_prompt(pid, boundary_id, workflow.get(pid)['revision'])
    info = boundaries.prompt_state(pid, boundary_id)
    assert info['llm_prompt'] is None
    assert info['effective_source'] == 'auto'


def test_prompt_generation_fails_fast_without_llm_key(boundary_project, monkeypatch):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    monkeypatch.setattr(boundaries, 'load_dotenv', lambda: None)
    monkeypatch.delenv('STUDIO_LLM_API_KEY', raising=False)
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('no request without a key'))
    with pytest.raises(ValueError, match='STUDIO_LLM_API_KEY'):
        boundaries.generate_prompt(pid, boundary_id, 'gpt-4o-mini', 0.1,
                                   confirm_paid=True, request_id='no-key',
                                   revision=state['revision'])
    assert boundaries.list_prompt_attempts(pid) == []


def test_prompt_roles_for_opening_and_closing(boundary_project):
    pid, state = boundary_project
    opening = boundaries.preview_prompt(pid, state['boundaries'][0]['id'], 'gpt-4o-mini')
    closing = boundaries.preview_prompt(pid, state['boundaries'][2]['id'], 'gpt-4o-mini')
    assert opening['payload']['role'] == 'opening'
    assert closing['payload']['role'] == 'closing'


def test_clear_prompt_acknowledges_inflight_attempt(boundary_project):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    with core.connect() as db:
        db.execute('INSERT INTO guided_boundary_prompt_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   ('inflight', pid, boundary_id, 'req-inflight', 'SUBMITTING', 'gpt-4o-mini',
                    0.1, 'sha', None, None, core.now()))
    boundaries.clear_prompt(pid, boundary_id, workflow.get(pid)['revision'])
    assert boundaries.list_prompt_attempts(pid)[0]['status'] == 'ACKNOWLEDGED'


def test_prompt_is_scoped_to_the_project(boundary_project):
    pid, state = boundary_project
    with pytest.raises(ValueError, match='Unknown boundary'):
        boundaries.prompt_state('not-a-project', state['boundaries'][1]['id'])


def test_prompt_routes(boundary_project, llm):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    base = f'/api/projects/{pid}/guided'
    api = TestClient(webapp.app)
    assert api.get(base + f'/boundaries/{boundary_id}/prompt').status_code == 200
    preview = api.post(base + f'/boundaries/{boundary_id}/prompt/preview', json={'model': 'gpt-4o-mini'})
    assert preview.status_code == 200
    assert preview.json()['estimated_cost_usd'] is None
    assert api.post(base + f'/boundaries/{boundary_id}/prompt',
                    json={'model': 'gpt-4o-mini', 'revision': state['revision']}).status_code == 400
    response = api.post(base + f'/boundaries/{boundary_id}/prompt',
                        json={'model': 'gpt-4o-mini', 'budget_usd': 0.1, 'confirm_paid': True,
                              'request_id': 'route-1', 'revision': state['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'COMPLETED'
    info = api.get(base + f'/boundaries/{boundary_id}/prompt').json()
    assert info['effective_source'] == 'llm'
    cleared = api.request('DELETE', base + f'/boundaries/{boundary_id}/prompt',
                          json={'revision': workflow.get(pid)['revision']})
    assert cleared.status_code == 200
    assert api.get(base + f'/boundaries/{boundary_id}/prompt').json()['effective_source'] == 'auto'


def test_prompt_resolve_route(boundary_project, monkeypatch):
    pid, state = boundary_project
    boundary_id = state['boundaries'][1]['id']
    monkeypatch.setattr(core, '_llm_json', lambda *args: (_ for _ in ()).throw(RuntimeError('network')))
    api = TestClient(webapp.app)
    base = f'/api/projects/{pid}/guided'
    response = api.post(base + f'/boundaries/{boundary_id}/prompt',
                        json={'model': 'gpt-4o-mini', 'budget_usd': 0.1, 'confirm_paid': True,
                              'request_id': 'route-unknown', 'revision': state['revision']})
    assert response.json()['status'] == 'UNKNOWN'
    resolved = api.post(base + f'/prompt-attempts/{response.json()["id"]}/resolve',
                        json={'confirm_checked': True})
    assert resolved.status_code == 200
    assert resolved.json()['status'] == 'ACKNOWLEDGED'
