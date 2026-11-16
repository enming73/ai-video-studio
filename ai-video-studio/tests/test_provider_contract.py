"""Provider errors must be actionable: whether money may already have been spent."""
import math
from dataclasses import FrozenInstanceError

import httpx
import pytest

from video_provider_core.contract import (
    Capability, CostEstimate, ProviderError, RemoteJob, RemoteStatus,
    VideoRequest, is_retryable, to_micro,
)
from video_provider_core.providers import request_json, bearer


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('method,status,kind,state', [
    ('POST', 401, 'auth', 'not_sent'),
    ('POST', 403, 'auth', 'not_sent'),
    ('POST', 400, 'invalid_request', 'not_sent'),
    ('POST', 422, 'invalid_request', 'not_sent'),
    ('POST', 404, 'unknown_remote', 'maybe_sent'),
    ('POST', 409, 'unknown_remote', 'maybe_sent'),
    ('GET', 404, 'invalid_request', 'not_sent'),
    ('GET', 409, 'invalid_request', 'not_sent'),
    ('POST', 429, 'rate_limited', 'not_sent'),
    ('GET', 429, 'rate_limited', 'not_sent'),
    ('POST', 500, 'transient', 'maybe_sent'),
    ('POST', 408, 'transient', 'maybe_sent'),
    ('GET', 503, 'transient', 'not_sent'),
])
def test_http_status_classification(method, status, kind, state):
    with client_for(lambda request: httpx.Response(status, text='vendor detail')) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, method, 'https://provider.example/v1')
    assert caught.value.kind == kind
    assert caught.value.submission_state == state
    assert 'vendor detail' in str(caught.value)


def test_rate_limit_parses_retry_after():
    with client_for(lambda request: httpx.Response(
            429, text='slow down', headers={'Retry-After': '12'})) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1')
    assert caught.value.retry_after_sec == 12
    with client_for(lambda request: httpx.Response(
            429, text='later', headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1')
    assert caught.value.retry_after_sec is None


def test_timeout_on_post_is_unknown_remote():
    def timeout(request):
        raise httpx.ConnectTimeout('no response', request=request)

    with client_for(timeout) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1')
        assert caught.value.kind == 'unknown_remote'
        assert caught.value.submission_state == 'maybe_sent'
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'GET', 'https://provider.example/v1')
        assert caught.value.submission_state == 'not_sent'


def test_non_json_post_is_unknown_remote():
    with client_for(lambda request: httpx.Response(200, text='plain text')) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1')
    assert caught.value.kind == 'unknown_remote'
    assert caught.value.submission_state == 'maybe_sent'


def test_error_text_is_truncated_and_has_no_headers():
    secret = 'super-secret-token'
    with client_for(lambda request: httpx.Response(500, text='X' * 10_000)) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1',
                         headers={'Authorization': 'Bearer ' + secret})
    text = str(caught.value)
    assert len(text) < 1_500
    assert secret not in text
    assert 'Authorization' not in text


def test_missing_api_key_is_auth_error():
    with pytest.raises(ProviderError) as caught:
        bearer(None)
    assert caught.value.kind == 'auth'
    assert caught.value.submission_state == 'not_sent'


def test_contract_types_are_frozen_and_defaulted():
    request = VideoRequest('a prompt', 5, 1280, 720)
    assert request.model == '' and request.first_frame is None
    with pytest.raises(FrozenInstanceError):
        request.duration_sec = 6
    assert RemoteJob('minimax', 'abc').remote_id == 'abc'
    assert RemoteStatus('running').url is None
    assert CostEstimate(1, 'v1', 'env').amount_microusd == 1
    assert Capability((4,), ('720p',), True, True, False, False, 'doc').durations == (4,)


def test_provider_error_requires_explicit_classification():
    with pytest.raises(TypeError):
        ProviderError('unclassified')
    unknown = ProviderError('x', kind='unknown_remote')
    assert unknown.submission_state == 'maybe_sent'


def test_retry_predicate_requires_not_sent():
    assert is_retryable(ProviderError('x', kind='transient', submission_state='not_sent'))
    assert is_retryable(ProviderError('x', kind='rate_limited', submission_state='not_sent'))
    assert not is_retryable(ProviderError('x', kind='transient', submission_state='maybe_sent'))
    assert not is_retryable(ProviderError('x', kind='unknown_remote',
                                          submission_state='maybe_sent'))
    assert not is_retryable(ProviderError('x', kind='auth', submission_state='not_sent'))


def test_money_conversion_in_contract():
    assert to_micro('0.1') == 100_000
    assert to_micro(1e-7) == 0
    for bad in (True, float('nan'), float('inf'), -1, '-0.1', 'abc'):
        with pytest.raises(ValueError):
            to_micro(bad)
    assert math.isfinite(to_micro('0.000001'))


def test_provider_capabilities_contract():
    from video_provider_core.providers import Flux, MiniMax, Wan
    minimax = MiniMax(None).capabilities(model='MiniMax-H3')
    assert minimax.durations == tuple(range(4, 16))
    assert minimax.supports_first_frame is False
    assert MiniMax(None).capabilities(i2v=True).supports_first_frame is True
    flux = Flux(None).capabilities(model='blackforestlabs/flux-3-max/text-to-video')
    assert flux.durations == tuple(range(5, 21))
    assert Flux(None).capabilities(i2v=True).supports_last_frame is True
    wan = Wan(None).capabilities(model='wan3.0-video')
    assert wan.durations == tuple(range(2, 31))
    assert Wan(None).capabilities(i2v=True) is None
    assert MiniMax(None).capabilities(model='unknown') is None


def test_estimate_cost_uses_verified_rate(monkeypatch):
    from video_provider_core.providers import Flux, MiniMax, Wan
    monkeypatch.delenv('MINIMAX_USD_PER_SECOND', raising=False)
    monkeypatch.delenv('FAL_USD_PER_SECOND', raising=False)
    monkeypatch.delenv('WAN_USD_PER_SECOND', raising=False)
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    request = VideoRequest('p', 5, 1280, 720)
    estimate = MiniMax(None).estimate_cost(request)
    assert estimate.amount_microusd == 500_000
    assert estimate.pricing_version == 'MINIMAX_USD_PER_SECOND:0.1'
    assert estimate.source == 'MINIMAX_USD_PER_SECOND'
    monkeypatch.setenv('FAL_USD_PER_SECOND', '0.2')
    assert Flux(None).estimate_cost(request).amount_microusd == 1_000_000
    monkeypatch.setenv('WAN_USD_PER_SECOND', '0.05')
    assert Wan(None).estimate_cost(request).amount_microusd == 250_000


def test_missing_rate_returns_none_estimate(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.delenv('MINIMAX_USD_PER_SECOND', raising=False)
    assert MiniMax(None).estimate_cost(VideoRequest('p', 5, 1280, 720)) is None


@pytest.mark.parametrize('rate', ['-1', 'abc', '200', '0'])
def test_invalid_rate_is_rejected(monkeypatch, rate):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', rate)
    with pytest.raises(ValueError):
        MiniMax(None).estimate_cost(VideoRequest('p', 5, 1280, 720))


def test_submit_video_rejects_unsupported_duration(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'task_id': 'x'})
    with client_for(handler) as client:
        with pytest.raises(ProviderError) as caught:
            MiniMax(client).submit_video(VideoRequest('p', 3, 1280, 720), 'idem')
    assert caught.value.kind == 'invalid_request'
    assert caught.value.submission_state == 'not_sent'
    assert calls == []


def test_submit_video_validates_frames_before_sending(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'task_id': 'x'})

    invalid = [
        VideoRequest('p', 5, 1280, 720, last_frame=b'last', last_frame_mime='image/png'),
        VideoRequest('p', 5, 1280, 720, first_frame=b'', first_frame_mime='image/png'),
        VideoRequest('p', 5, 1280, 720, first_frame=b'first', first_frame_mime='image/gif'),
    ]
    with client_for(handler) as client:
        for request in invalid:
            with pytest.raises(ProviderError) as caught:
                MiniMax(client).submit_video(request, 'idem')
            assert caught.value.kind == 'invalid_request'
            assert caught.value.submission_state == 'not_sent'
    assert calls == []


def test_submit_video_rejects_unsupported_resolution(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'task_id': 'x'})

    with client_for(handler) as client:
        with pytest.raises(ProviderError, match='resolution'):
            MiniMax(client).submit_video(VideoRequest('p', 5, 1920, 1080), 'idem')
        assert calls == []
        assert MiniMax(client).submit_video(VideoRequest('p', 5, 1280, 720), 'idem').remote_id


def test_submit_video_dispatches_first_and_last_frames(monkeypatch):
    import json
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'task_id': 'i2v'})

    request = VideoRequest('p', 5, 1280, 720, first_frame=b'first',
                           first_frame_mime='image/png', last_frame=b'last',
                           last_frame_mime='image/webp')
    with client_for(handler) as client:
        job = MiniMax(client).submit_video(request, 'idem')
    assert job.remote_id == 'i2v'
    roles = [part.get('role') for part in bodies[0]['content'] if part['type'] == 'image_url']
    assert roles == ['first_frame', 'last_frame']


def test_adapter_error_payload_is_bounded(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    with client_for(lambda request: httpx.Response(200, json={'detail': 'X' * 10_000})) as client:
        with pytest.raises(ProviderError) as caught:
            MiniMax(client).submit('p', 5)
    assert len(str(caught.value)) < 1_500
    assert caught.value.kind == 'unknown_remote'


@pytest.mark.parametrize('message,kind', [
    ('invalid api key', 'auth'),
    ('rate limit exceeded', 'rate_limited'),
    ('content policy violation', 'content_rejected'),
    ('internal server error', 'transient'),
    ('unknown vendor problem', 'invalid_request'),
])
def test_vendor_envelope_is_classified(message, kind):
    def handler(request):
        return httpx.Response(200, json={'base_resp': {'status_code': 1000, 'status_msg': message}})
    with client_for(handler) as client:
        with pytest.raises(ProviderError) as caught:
            request_json(client, 'POST', 'https://provider.example/v1')
    assert caught.value.kind == kind
    assert caught.value.submission_state == 'not_sent'


def test_flux_capability_sources_are_model_specific():
    from video_provider_core.providers import Flux
    t2v = 'https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=blackforestlabs%2Fflux-3-max%2Ftext-to-video'
    i2v = 'https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=blackforestlabs%2Fflux-3-max%2Fimage-to-video'
    assert Flux(None).capabilities().source == t2v
    assert Flux(None).capabilities(i2v=True).source == i2v


def test_flux_and_wan_status_mapping(monkeypatch):
    from video_provider_core.providers import Flux, Wan
    monkeypatch.setenv('FAL_KEY', 'key')
    with client_for(lambda request: httpx.Response(200, json={'status': 'IN_PROGRESS'})) as client:
        assert Flux(client).status(RemoteJob('flux', 'id')).state == 'running'
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'key')
    monkeypatch.setenv('WAN_BASE_URL', 'https://workspace.eu-central-1.maas.aliyuncs.com')
    with client_for(lambda request: httpx.Response(
            200, json={'output': {'task_status': 'PENDING'}})) as client:
        assert Wan(client).status(RemoteJob('wan', 'id')).state == 'running'


def test_submit_video_wraps_legacy_result(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    with client_for(lambda request: httpx.Response(200, json={'task_id': '123'})) as client:
        job = MiniMax(client).submit_video(
            VideoRequest('p', 5, 1280, 720, model='MiniMax-H3'), 'idem')
    assert job == RemoteJob('minimax', '123')


@pytest.mark.parametrize('payload,state', [
    ({'task': {'status': 'running'}}, 'running'),
    ({'task': {'status': 'succeeded', 'content': {'url': 'https://cdn.example/v.mp4'}}},
     'succeeded'),
    ({'task': {'status': 'failed', 'error': 'rejected'}}, 'failed'),
])
def test_status_maps_legacy_poll(monkeypatch, payload, state):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        result = MiniMax(client).status(RemoteJob('minimax', '123'))
    assert result.state == state
    if state == 'succeeded':
        assert result.url == 'https://cdn.example/v.mp4'


def test_legacy_submit_and_poll_remain_available(monkeypatch):
    from video_provider_core.providers import MiniMax
    monkeypatch.setenv('MINIMAX_API_KEY', 'key')
    responses = iter([
        {'task_id': 'legacy'},
        {'task': {'status': 'succeeded', 'content': {'url': 'https://cdn/v.mp4'}}},
    ])
    with client_for(lambda request: httpx.Response(200, json=next(responses))) as client:
        provider = MiniMax(client)
        assert provider.submit('p', 5) == 'legacy'
        assert provider.poll('legacy').url == 'https://cdn/v.mp4'
