"""Provider errors must be actionable: whether money may already have been spent."""
import math

import httpx
import pytest

from video_provider_core.contract import (
    Capability, CostEstimate, ProviderError, RemoteJob, RemoteStatus,
    VideoRequest, to_micro,
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
    with pytest.raises(Exception):
        request.duration_sec = 6
    assert RemoteJob('minimax', 'abc').remote_id == 'abc'
    assert RemoteStatus('running').url is None
    assert CostEstimate(1, 'v1', 'env').amount_microusd == 1
    assert Capability((4,), ('720p',), True, True, False, False, 'doc').durations == (4,)


def test_money_conversion_in_contract():
    assert to_micro('0.1') == 100_000
    assert to_micro(1e-7) == 0
    for bad in (True, float('nan'), float('inf'), -1, '-0.1', 'abc'):
        with pytest.raises(ValueError):
            to_micro(bad)
    assert math.isfinite(to_micro('0.000001'))
