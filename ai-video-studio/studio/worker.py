"""Persistent worker loop: lease, heartbeat, recovery and handler dispatch."""
from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

from . import attempts, jobs, ledger
from video_provider_core import ProviderError, is_retryable

HANDLERS: dict[str, object] = {}


class JobStopped(Exception):
    def __init__(self, action: str):
        self.action = action
        super().__init__(action)


class JobBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class Control:
    def __init__(self, lease_lost=None):
        self.lease_lost = lease_lost

    def checkpoint(self, job_id: str):
        if self.lease_lost is not None and self.lease_lost.is_set():
            raise jobs.LeaseLost('Worker no longer owns the job')
        action = jobs.stop_requested(job_id)
        if action:
            raise JobStopped(action)

    def block(self, reason: str):
        raise JobBlocked(reason)


def register(kind: str, handler) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError('Handler kind is required')
    HANDLERS[kind] = handler


def _finish_blocked(job_id: str, detail: str, reason: str = 'unknown_remote',
                    owner: str | None = None) -> None:
    attempt = jobs.latest_attempt(job_id)
    if attempt and attempt['status'] == 'SUBMITTING':
        attempts.mark_unknown(attempt['id'], detail)
    jobs.set_status(job_id, 'BLOCKED', block_reason=reason,
                    error_class=reason, error_detail=detail, owner=owner)


def run_once(owner: str | None = None, *, lease_seconds: int = 60,
             heartbeat_seconds: float = 15) -> str | None:
    owner = owner or uuid4().hex
    job = jobs.claim(owner, lease_seconds=lease_seconds)
    if job is None:
        return None
    job_id = job['id']
    stopped = threading.Event()
    lease_lost = threading.Event()

    def beat():
        while not stopped.wait(heartbeat_seconds):
            if not jobs.heartbeat(job_id, owner, lease_seconds=lease_seconds):
                lease_lost.set()
                return

    beat_thread = threading.Thread(target=beat, daemon=True)
    beat_thread.start()
    control = Control(lease_lost)
    payload = json.loads(job['payload_json'])
    try:
        control.checkpoint(job_id)
        action = jobs.recovery_action(job_id,
                                      max_attempts=payload.get('max_attempts', 2))
        if action == 'block_unknown':
            _finish_blocked(job_id, 'Previous submission state is unknown', owner=owner)
        elif action == 'complete':
            attempt = jobs.latest_attempt(job_id)
            output = json.loads(attempt['output_json'] or '{}')
            jobs.set_status(job_id, 'SUCCEEDED', output=output, owner=owner)
        elif action == 'fail':
            attempt = jobs.latest_attempt(job_id)
            jobs.set_status(job_id, 'FAILED', error_class='attempt_failed',
                            error_detail=(attempt or {}).get('error_detail'), owner=owner)
        else:
            handler = HANDLERS.get(job['kind'])
            if handler is None:
                jobs.set_status(job_id, 'FAILED', error_class='invalid_request',
                                error_detail='No handler registered', owner=owner)
            else:
                if action == 'resume':
                    attempt = jobs.latest_attempt(job_id)
                    result = handler.resume(job, attempt, control)
                else:
                    result = handler.execute(job, attempts, control)
                if lease_lost.is_set():
                    raise jobs.LeaseLost('Worker no longer owns the job')
                jobs.set_status(job_id, 'SUCCEEDED', output=result or {}, owner=owner)
                if payload.get('guided_request_id'):
                    from . import guided_compat
                    try:
                        guided_compat.reconcile_outputs(job['project_id'])
                    except Exception as exc:
                        jobs.append_event(job_id, 'guided_reconcile_failed',
                                          {'error': str(exc)[:500]})
    except jobs.LeaseLost:
        jobs.append_event(job_id, 'lease_lost', {'owner': owner})
    except JobStopped as stop:
        warning = {'warning': 'remote may continue and bill'}
        jobs.set_status(job_id, 'PAUSED' if stop.action == 'pause' else 'CANCELLED',
                        output=warning, owner=owner)
    except JobBlocked as blocked:
        jobs.set_status(job_id, 'BLOCKED', block_reason=blocked.reason, owner=owner)
    except ProviderError as exc:
        if lease_lost.is_set():
            jobs.append_event(job_id, 'lease_lost', {'owner': owner})
        elif exc.kind in ('unknown_remote', 'auth'):
            _finish_blocked(job_id, str(exc),
                            'unknown_remote' if exc.kind == 'unknown_remote' else 'auth',
                            owner=owner)
        elif is_retryable(exc):
            attempt = jobs.latest_attempt(job_id)
            count = len(attempts.list_for_job(job_id))
            if attempt and attempt['status'] == 'FAILED_RETRYABLE' and count < payload.get('max_attempts', 2):
                jobs.set_status(job_id, 'PENDING', error_class=exc.kind,
                                error_detail=str(exc)[:1200], owner=owner)
            else:
                jobs.set_status(job_id, 'FAILED', error_class=exc.kind,
                                error_detail=str(exc)[:1200], owner=owner)
        else:
            jobs.set_status(job_id, 'FAILED', error_class=exc.kind,
                            error_detail=str(exc)[:1200], owner=owner)
    except ledger.BudgetExceeded as exc:
        jobs.set_status(job_id, 'BLOCKED', block_reason='budget_exceeded',
                        error_detail=str(exc), owner=owner)
    except Exception as exc:
        attempt = jobs.latest_attempt(job_id)
        if attempt and attempt['status'] == 'SUBMITTING':
            _finish_blocked(job_id, str(exc), 'unknown_remote', owner=owner)
        else:
            jobs.set_status(job_id, 'FAILED', error_class=type(exc).__name__,
                            error_detail=str(exc)[:1200], owner=owner)
    finally:
        stopped.set()
        beat_thread.join(timeout=2)
        jobs.release(job_id, owner)
    return job_id


def run_forever(owner: str | None = None, poll_seconds: float = 2,
                stop_event: threading.Event | None = None) -> None:
    from .handlers import register_all
    if not HANDLERS:
        register_all()
    while stop_event is None or not stop_event.is_set():
        try:
            worked = run_once(owner) is not None
        except Exception:
            worked = False
        if not worked:
            if stop_event is not None:
                if stop_event.wait(poll_seconds):
                    break
            else:
                time.sleep(poll_seconds)


_embedded_lock = threading.Lock()
_embedded_thread: threading.Thread | None = None
_embedded_stop: threading.Event | None = None


def start_embedded(owner: str = 'embedded', *, poll_seconds: float = 2) -> threading.Thread:
    global _embedded_thread, _embedded_stop
    with _embedded_lock:
        if _embedded_thread is not None and _embedded_thread.is_alive():
            return _embedded_thread
        stop = threading.Event()
        thread = threading.Thread(target=run_forever, args=(owner, poll_seconds, stop),
                                  name='studio-embedded-worker', daemon=True)
        _embedded_thread, _embedded_stop = thread, stop
        thread.start()
        return thread


def stop_embedded(timeout: float = 5) -> None:
    global _embedded_thread, _embedded_stop
    with _embedded_lock:
        thread, stop = _embedded_thread, _embedded_stop
        _embedded_thread = _embedded_stop = None
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout)
