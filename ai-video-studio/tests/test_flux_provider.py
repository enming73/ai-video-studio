import httpx
from video_provider_core.providers import Flux


def test_flux_poll_uses_app_queue_route(monkeypatch):
    monkeypatch.setenv('FAL_KEY', 'fake')
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'status': 'IN_PROGRESS'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert Flux(client).poll('task-1').status == 'running'
    assert paths == ['/blackforestlabs/flux-3-max/requests/task-1/status']


def test_flux_i2v_completed_fetches_result_from_app_queue_route(monkeypatch):
    monkeypatch.setenv('FAL_KEY', 'fake')
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/status'):
            return httpx.Response(200, json={'status': 'COMPLETED'})
        return httpx.Response(200, json={'video': {'url': 'https://example.com/video.mp4'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Flux(client).poll('task-1', model='blackforestlabs/flux-3-max/image-to-video')
    assert result.url == 'https://example.com/video.mp4'
    assert paths == ['/blackforestlabs/flux-3-max/requests/task-1/status',
                     '/blackforestlabs/flux-3-max/requests/task-1']
