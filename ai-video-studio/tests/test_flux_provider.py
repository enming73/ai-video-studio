import httpx
from video_provider_core.providers import Flux


def test_flux_poll_uses_text_to_video_route(monkeypatch):
    monkeypatch.setenv('FAL_KEY', 'fake')
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'status': 'IN_PROGRESS'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert Flux(client).poll('task-1').status == 'running'
    assert paths == ['/blackforestlabs/flux-3-max/text-to-video/requests/task-1/status']
