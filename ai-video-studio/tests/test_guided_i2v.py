import base64
import httpx
import pytest
from video_provider_core.providers import MiniMax
from studio import core, guided_workflow as workflow, guided_characters as characters, guided_generation as generation


def test_minimax_i2v_payload_contains_exact_first_frame(monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'fake')
    seen = []
    def handler(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'task_id': '123'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert MiniMax(client).submit_i2v('A forest', 5, b'exact-image', 'image/png') == '123'
    image = seen[0]['content'][1]
    assert image['role'] == 'first_frame'
    assert base64.b64decode(image['image_url']['url'].split(',')[1]) == b'exact-image'


def test_enabled_guided_project_rejects_video_before_confirmed_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Empty forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = characters.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A river flows beneath the trees.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    workflow.confirm_storyboard(pid, state['revision'])
    with pytest.raises(ValueError, match='frame|model'):
        generation.preview(pid, 1)
