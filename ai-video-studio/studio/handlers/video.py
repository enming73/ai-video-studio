"""Video job handler: submit, poll, download and validate exactly once."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from video_provider_core import (
    PROVIDERS, ProviderError, RemoteJob, RemoteStatus, VideoRequest, is_retryable,
)
from video_provider_core.media import download

from .. import core, attempts as attempt_store

MAX_FRAME_BYTES = 20 * 1024 * 1024


def _frame(root: Path, path, mime):
    if path is None:
        return None, None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('Frame path is outside the project')
    if not resolved.is_file():
        raise ValueError('Frame is missing')
    if resolved.stat().st_size > MAX_FRAME_BYTES:
        raise ValueError('Frame exceeds 20 MiB')
    return resolved.read_bytes(), mime


def _provider(name: str, client):
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError('Unknown provider')
    return cls(client)


def _payload(job: dict) -> dict:
    return json.loads(job['payload_json'])


def _request(job: dict, payload: dict, root: Path) -> VideoRequest:
    first, first_mime = _frame(root, payload.get('first_frame_path'),
                               payload.get('first_mime'))
    last, last_mime = _frame(root, payload.get('last_frame_path'),
                             payload.get('last_mime'))
    return VideoRequest(
        prompt=payload['prompt'], duration_sec=payload['duration_sec'],
        width=payload['width'], height=payload['height'],
        model=payload.get('model', ''), first_frame=first,
        first_frame_mime=first_mime, last_frame=last, last_frame_mime=last_mime)


def _reservation(job: dict, payload: dict, provider, request):
    estimate = provider.estimate_cost(request)
    if estimate is not None:
        return estimate.amount_microusd, estimate.pricing_version
    ceiling = payload.get('reserve_microusd')
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return None, None
    return ceiling, f'user_ceiling:{job["request_key"]}'


def _sleep(payload: dict):
    delay = payload.get('poll_interval_sec', 5)
    if delay and delay > 0:
        time.sleep(delay)


def _finish(job, attempt, payload, provider, control, client):
    root = Path(core.HOME) / job['project_id']
    if attempt['status'] == 'SUBMITTED':
        attempt = attempt_store.mark_status(attempt['id'], 'POLLING')
    remote_id = attempt['remote_id']
    while True:
        control.checkpoint(job['id'])
        try:
            status = provider.status(RemoteJob(payload['provider'], remote_id),
                                     model=payload.get('model'))
        except ProviderError as exc:
            attempt_store.mark_unknown(attempt['id'], str(exc))
            control.block('unknown_remote')
        if status.state == 'failed':
            raise RuntimeError(status.error_detail or 'Provider reported failure')
        if status.state == 'succeeded':
            break
        if status.state in ('cancelled', 'unknown'):
            raise RuntimeError(f'Provider status {status.state}')
        _sleep(payload)
    attempt_store.mark_status(attempt['id'], 'DOWNLOADING')
    target = root / 'jobs' / job['id'] / f'{uuid4().hex}.mp4'
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        download(client, status.url, target)
        attempt_store.mark_status(attempt['id'], 'VALIDATING')
        core.verify_video(target, payload['duration_sec'])
    except Exception:
        attempt_store.mark_failed(attempt['id'], 'FAILED_FINAL',
                                  'Local download or validation failed')
        raise
    output = {
        'file': str(target),
        'remote_id': remote_id,
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    attempt_store.mark_completed(attempt['id'], output)
    return output


def execute(job, attempts, control):
    payload = _payload(job)
    root = Path(core.HOME) / job['project_id']
    request = _request(job, payload, root)
    with httpx.Client(timeout=httpx.Timeout(40, connect=15),
                      follow_redirects=False) as client:
        provider = _provider(payload['provider'], client)
        amount, pricing = _reservation(job, payload, provider, request)
        if amount is None:
            control.block('pricing_missing')
        attempt = attempts.create(job['id'], provider=payload['provider'],
                                  model=payload.get('model', ''), input_data=payload)
        attempts.prepare_submission(attempt['id'], amount_microusd=amount,
                                    pricing_version=pricing)
        try:
            remote = provider.submit_video(request, attempt['idempotency_key'])
        except ProviderError as exc:
            if exc.kind == 'unknown_remote' or exc.submission_state == 'maybe_sent':
                attempts.mark_unknown(attempt['id'], str(exc))
                control.block('unknown_remote')
            attempts.mark_failed(
                attempt['id'],
                'FAILED_RETRYABLE' if is_retryable(exc) else 'FAILED_FINAL',
                str(exc))
            raise
        attempts.attach_remote(attempt['id'], remote.remote_id)
        attempt = attempts.get(attempt['id'])
        return _finish(job, attempt, payload, provider, control, client)


def resume(job, attempt, control):
    payload = _payload(job)
    if not attempt or not attempt.get('remote_id'):
        control.block('unknown_remote')
    root = Path(core.HOME) / job['project_id']
    with httpx.Client(timeout=httpx.Timeout(40, connect=15),
                      follow_redirects=False) as client:
        provider = _provider(payload['provider'], client)
        if attempt['status'] == 'SUBMITTED':
            from .. import attempts as attempt_service
            attempt = attempt_service.mark_status(attempt['id'], 'POLLING')
        return _finish(job, attempt, payload, provider, control, client)
