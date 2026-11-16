"""Shared enqueue helper for Guided paid request jobs.

One request maps to one Job with an immutable payload. Replays and double
clicks resolve to the same Job via the request key or a request-alias event,
and an unresolved target reuses its existing Job so a second click can never
open a second paid run for the same character, boundary, or storyboard.
"""
from __future__ import annotations

import math

from . import core, job_runner, jobs, ledger

REUSABLE_STATUSES = ('PENDING', 'RUNNING', 'PAUSED')
REUSABLE_BLOCK_REASON = 'unknown_remote'


def _find_by_key(conn, pid, request_key):
    row = conn.execute('SELECT * FROM jobs WHERE project_id=? AND request_key=?',
                       (pid, request_key)).fetchone()
    return dict(row) if row else None


def _find_by_alias(conn, pid, request_id):
    row = conn.execute(
        "SELECT j.* FROM jobs j JOIN job_events e ON e.job_id=j.id "
        "WHERE j.project_id=? AND e.type='request_alias' "
        "AND json_extract(e.data_json,'$.request_id')=? "
        'ORDER BY j.created DESC LIMIT 1', (pid, request_id)).fetchone()
    return dict(row) if row else None


def find(pid: str, *, kind: str, request_id: str, db=None) -> dict | None:
    """Find the Job for a request ID by its request key or an alias event."""
    conn = db or core.connect()
    try:
        job = _find_by_key(conn, pid, f'{kind}:{request_id}')
        if job is None:
            job = _find_by_alias(conn, pid, request_id)
        if job is not None and job['kind'] != kind:
            return None
        return job
    finally:
        if db is None:
            conn.close()


def latest_attempt(job: dict, *, db=None) -> dict | None:
    return jobs.latest_attempt(job['id'], db=db)


def _remember_alias(db, job, request_id):
    if f'{job["kind"]}:{request_id}' == job['request_key']:
        return
    exists = db.execute(
        "SELECT 1 FROM job_events WHERE job_id=? AND type='request_alias' "
        "AND json_extract(data_json,'$.request_id')=?", (job['id'], request_id)).fetchone()
    if not exists:
        jobs.append_event(job['id'], 'request_alias', {'request_id': request_id}, db=db)


def submit(pid: str, *, kind: str, request_id: str, payload: dict,
           budget_usd: float, target_key: str | None = None,
           run_key: str | None = None) -> dict:
    if isinstance(budget_usd, bool) or not isinstance(budget_usd, (int, float)) \
            or not math.isfinite(budget_usd) or budget_usd <= 0:
        raise ValueError('Budget must be a positive finite number')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    if not isinstance(kind, str) or not kind:
        raise ValueError('Job kind is required')
    if not isinstance(payload, dict):
        raise ValueError('Payload must be an object')
    request_key = f'{kind}:{request_id}'
    if run_key is None:
        # One Job per request uses its own Run; a batch shares one caller's Run.
        run_key = request_key
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        core.project(db, pid)
        existing = _find_by_key(db, pid, request_key) or _find_by_alias(db, pid, request_id)
        if existing is not None:
            if existing['kind'] != kind or existing['target_key'] != target_key:
                raise ValueError('Request id belongs to another operation')
            return existing
        if target_key is not None:
            # Reuse only genuinely unresolved targets. A BLOCKED Job is only
            # unresolved when the provider outcome is unknown; budget/auth
            # blocks are terminal from the caller's view and a new request must
            # be able to create a fresh Job once the budget is raised.
            placeholders = ','.join('?' for _ in REUSABLE_STATUSES)
            unresolved = db.execute(
                'SELECT * FROM jobs WHERE project_id=? AND target_key=? '
                f'AND (status IN ({placeholders}) '
                "OR (status='BLOCKED' AND block_reason=?)) "
                'ORDER BY created DESC LIMIT 1',
                (pid, target_key, *REUSABLE_STATUSES, REUSABLE_BLOCK_REASON)).fetchone()
            if unresolved is not None:
                job = dict(unresolved)
                _remember_alias(db, job, request_id)
                return job
        run_id = job_runner.open_or_reuse_run(
            pid, request_key=run_key,
            budget_microusd=ledger.to_micro(budget_usd), db=db)
        return jobs.create(pid, kind=kind, payload=payload, request_key=request_key,
                           run_id=run_id, target_key=target_key, db=db)
