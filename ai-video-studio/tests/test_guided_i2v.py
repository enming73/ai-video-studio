import base64
import httpx
import pytest
from video_provider_core.providers import MiniMax, Flux
from studio import core, guided_workflow as workflow, guided_characters as characters, guided_generation as generation, provider_capabilities


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


def test_minimax_i2v_payload_contains_first_and_last_frame(monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'fake')
    seen = []
    def handler(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'task_id': '123'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert MiniMax(client).submit_i2v(
            'A forest', 5, b'exact-first', 'image/png', b'exact-last', 'image/webp') == '123'
    payload = seen[0]
    assert payload['ratio'] == 'adaptive'
    assert [item.get('role') for item in payload['content'][1:]] == ['first_frame', 'last_frame']
    first, last = payload['content'][1], payload['content'][2]
    assert first['image_url']['url'].startswith('data:image/png;base64,')
    assert last['image_url']['url'].startswith('data:image/webp;base64,')
    assert base64.b64decode(first['image_url']['url'].split(',')[1]) == b'exact-first'
    assert base64.b64decode(last['image_url']['url'].split(',')[1]) == b'exact-last'


def test_last_frame_requires_first_frame_and_supported_type(monkeypatch):
    monkeypatch.setenv('MINIMAX_API_KEY', 'fake')
    def handler(request):
        raise AssertionError('validation must not reach the network')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = MiniMax(client)
        with pytest.raises(ValueError, match='first'):
            provider.submit_i2v('A forest', 5, b'', 'image/png', b'last', 'image/png')
        with pytest.raises(ValueError, match='last'):
            provider.submit_i2v('A forest', 5, b'first', 'image/png', b'last', 'image/gif')


def test_minimax_capability_declares_first_last_frame():
    assert provider_capabilities.i2v_capabilities('minimax', 'MiniMax-H3')['first_last_frame'] is True
    assert provider_capabilities.i2v_capabilities('mock', 'mock')['first_last_frame'] is True
    assert provider_capabilities.i2v_capabilities('flux', 'x')['verified'] is False


def test_flux_i2v_capability():
    cap = provider_capabilities.i2v_capabilities('flux', 'blackforestlabs/flux-3-max/image-to-video')
    assert cap['verified'] and cap['first_last_frame']
    assert cap['durations'] == list(range(5, 21))


def test_flux_i2v_submits_uploaded_boundary_frames_and_polls_i2v_route(monkeypatch):
    import json
    import sys
    import types
    monkeypatch.setenv('FAL_KEY', 'fake')
    monkeypatch.setitem(sys.modules, 'fal_client', types.SimpleNamespace(
        upload=lambda data, mime: 'https://v3b.fal.media/' + data.decode()))
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'POST':
            return httpx.Response(200, json={'request_id': '123'})
        return httpx.Response(200, json={'status': 'IN_PROGRESS'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = Flux(client)
        assert provider.submit_i2v('A forest', 5, b'first', 'image/png', b'last', 'image/webp') == '123'
        assert provider.poll('123', model='blackforestlabs/flux-3-max/image-to-video').status == 'running'
    assert requests[0].url.path == '/blackforestlabs/flux-3-max/image-to-video'
    payload = json.loads(requests[0].content)
    assert payload['image_url'] == 'https://v3b.fal.media/first'
    assert payload['end_image_url'] == 'https://v3b.fal.media/last'
    assert payload['duration'] == 5
    assert requests[1].url.path == '/blackforestlabs/flux-3-max/requests/123/status'


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
