"""Persistent worker loop: lease, heartbeat, recovery and handler dispatch."""
from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

from . import attempts, jobs
from video_provider_core import ProviderError

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
    def checkpoint(self, job_id: str):
        action = jobs.stop_requested(job_id)
        if action:
            raise JobStopped(action)

    def block(self, reason: str):
        raise JobBlocked(reason)


def register(kind: str, handler) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError('Handler kind is required')
    HANDLERS[kind] = handler


def _finish_blocked(job_id: str, detail: str) -> None:
    attempt = jobs.latest_attempt(job_id)
    if attempt and attempt['status'] == 'SUBMITTING':
        attempts.mark_unknown(attempt['id'], detail)
    jobs.set_status(job_id, 'BLOCKED', block_reason='unknown_remote',
                    error_class='unknown_remote', error_detail=detail)


def run_once(owner: str | None = None, *, lease_seconds: int = 60,
             heartbeat_seconds: float = 15) -> str | None:
    owner = owner or uuid4().hex
    job = jobs.claim(owner, lease_seconds=lease_seconds)
    if job is None:
        return None
    job_id = job['id']
    stopped = threading.Event()

    def beat():
        while not stopped.wait(heartbeat_seconds):
            if not jobs.heartbeat(job_id, owner, lease_seconds=lease_seconds):
                return

    beat_thread = threading.Thread(target=beat, daemon=True)
    beat_thread.start()
    control = Control()
    try:
        payload = json.loads(job['payload_json'])
        action = jobs.recovery_action(job_id,
                                      max_attempts=payload.get('max_attempts', 2))
        if action == 'block_unknown':
            _finish_blocked(job_id, 'Previous submission state is unknown')
        elif action == 'complete':
            attempt = jobs.latest_attempt(job_id)
            output = json.loads(attempt['output_json'] or '{}')
            jobs.set_status(job_id, 'SUCCEEDED', output=output)
        elif action == 'fail':
            attempt = jobs.latest_attempt(job_id)
            jobs.set_status(job_id, 'FAILED', error_class='attempt_failed',
                            error_detail=(attempt or {}).get('error_detail'))
        else:
            handler = HANDLERS.get(job['kind'])
            if handler is None:
                jobs.set_status(job_id, 'FAILED', error_class='invalid_request',
                                error_detail='No handler registered')
            else:
                if action == 'resume':
                    attempt = jobs.latest_attempt(job_id)
                    result = handler.resume(job, attempt, control)
                else:
                    result = handler.execute(job, attempts, control)
                jobs.set_status(job_id, 'SUCCEEDED', output=result or {})
    except JobStopped as stop:
        jobs.set_status(job_id, 'PAUSED' if stop.action == 'pause' else 'CANCELLED',
                        block_reason=None)
    except JobBlocked as blocked:
        jobs.set_status(job_id, 'BLOCKED', block_reason=blocked.reason)
    except ProviderError as exc:
        if exc.kind in ('unknown_remote', 'auth'):
            _finish_blocked(job_id, str(exc))
        else:
            jobs.set_status(job_id, 'FAILED', error_class=exc.kind,
                            error_detail=str(exc)[:1200])
    except Exception as exc:
        jobs.set_status(job_id, 'FAILED', error_class=type(exc).__name__,
                        error_detail=str(exc)[:1200])
    finally:
        stopped.set()
        beat_thread.join(timeout=2)
        jobs.release(job_id, owner)
    return job_id


def run_forever(owner: str | None = None, poll_seconds: float = 2) -> None:
    from .handlers import register_all
    register_all()
    while True:
        if run_once(owner) is None:
            time.sleep(poll_seconds)
