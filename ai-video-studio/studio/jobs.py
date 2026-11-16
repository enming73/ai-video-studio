"""Persistent job lifecycle, leases and recovery decisions."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import core

TERMINAL = ('SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED')
STATUSES = ('PENDING', 'RUNNING', 'PAUSED', *TERMINAL)
_TRANSITIONS = {
    'PENDING': {'RUNNING', 'PAUSED', 'CANCELLED', 'FAILED'},
    'RUNNING': {'PENDING', 'PAUSED', 'CANCELLED', 'SUCCEEDED', 'FAILED', 'BLOCKED'},
    'PAUSED': {'PENDING', 'CANCELLED', 'FAILED', 'BLOCKED'},
    'BLOCKED': {'SUCCEEDED'},
    **{status: set() for status in TERMINAL if status != 'BLOCKED'},
}


class LeaseLost(ValueError):
    pass


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


@contextmanager
def _tx(db=None):
    owned = db is None
    conn = core.connect() if owned else db
    try:
        if owned:
            conn.execute('BEGIN IMMEDIATE')
        yield conn
        if owned:
            conn.commit()
    except Exception:
        if owned:
            conn.rollback()
        raise
    finally:
        if owned:
            conn.close()


def _row(conn, job_id: str) -> dict:
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown job')
    return dict(row)


def get(job_id: str, db=None) -> dict:
    conn = db or core.connect()
    try:
        return _row(conn, job_id)
    finally:
        if db is None:
            conn.close()


def list_for_project(pid: str, *, status: str | None = None, limit: int = 100,
                     db=None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise ValueError('Invalid job status')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid limit')
    conn = db or core.connect()
    try:
        sql = 'SELECT * FROM jobs WHERE project_id=?'
        args = [pid]
        if status is not None:
            sql += ' AND status=?'
            args.append(status)
        sql += ' ORDER BY created,id LIMIT ?'
        args.append(limit)
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        if db is None:
            conn.close()


def create(pid: str, *, kind: str, payload: dict, request_key: str,
           run_id: str | None, budget_limit_microusd: int | None = None,
           priority: int = 0, target_key: str | None = None, db=None) -> dict:
    if not isinstance(kind, str) or not kind:
        raise ValueError('Job kind is required')
    if not isinstance(payload, dict):
        raise ValueError('Job payload must be an object')
    if not isinstance(request_key, str) or not request_key.strip():
        raise ValueError('Request key is required')
    if type(priority) is not int:
        raise ValueError('Priority must be an integer')
    if budget_limit_microusd is not None and (
            isinstance(budget_limit_microusd, bool)
            or not isinstance(budget_limit_microusd, int)
            or budget_limit_microusd < 0):
        raise ValueError('Budget must be a nonnegative integer')
    serialized = _canonical(payload)
    with _tx(db) as conn:
        core.project(conn, pid)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('An open run is required')
        run = conn.execute('SELECT project_id,status FROM runs WHERE id=?',
                           (run_id,)).fetchone()
        if run is None:
            raise ValueError('Unknown run')
        if run['project_id'] != pid:
            raise ValueError('Run belongs to another project')
        if run['status'] != 'OPEN':
            raise ValueError('Run is not open')
        existing = conn.execute(
            'SELECT * FROM jobs WHERE project_id=? AND request_key=?',
            (pid, request_key)).fetchone()
        if existing is not None:
            if existing['payload_json'] != serialized:
                raise ValueError('Request key reused with a different payload')
            return dict(existing)
        job_id = uuid4().hex
        now = core.now()
        conn.execute(
            'INSERT INTO jobs(id,project_id,run_id,kind,status,request_key,'
            'payload_json,budget_limit_microusd,priority,target_key,created,updated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, pid, run_id, kind, 'PENDING', request_key, serialized,
             budget_limit_microusd, priority, target_key, now, now))
        _append(conn, job_id, 'created', {'kind': kind, 'request_key': request_key})
        return _row(conn, job_id)


def _append(conn, job_id: str, event_type: str, data: dict,
            attempt_id: str | None = None) -> str:
    event_id = uuid4().hex
    conn.execute(
        'INSERT INTO job_events(id,job_id,attempt_id,type,data_json,created) '
        'VALUES (?,?,?,?,?,?)',
        (event_id, job_id, attempt_id, event_type,
         json.dumps(data, ensure_ascii=False, sort_keys=True), core.now()))
    return event_id


def append_event(job_id: str, event_type: str, data: dict,
                 attempt_id: str | None = None, db=None) -> str:
    with _tx(db) as conn:
        _row(conn, job_id)
        return _append(conn, job_id, event_type, data, attempt_id)


def latest_attempt(job_id: str, db=None) -> dict | None:
    conn = db or core.connect()
    try:
        row = conn.execute(
            'SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1',
            (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if db is None:
            conn.close()


def set_status(job_id: str, status: str, *, error_class: str | None = None,
               error_detail: str | None = None, block_reason: str | None = None,
               output: dict | None = None, owner: str | None = None, db=None) -> dict:
    if status not in STATUSES:
        raise ValueError('Invalid job status')
    with _tx(db) as conn:
        job = _row(conn, job_id)
        if status not in _TRANSITIONS[job['status']]:
            raise ValueError(f"Illegal job transition {job['status']} -> {status}")
        if owner is not None and (job['lease_owner'] != owner
                                  or job['status'] != 'RUNNING'):
            raise LeaseLost('Worker no longer owns the job')
        clause = ' AND lease_owner=?' if owner is not None else ''
        values = (status, error_class, error_detail, block_reason,
                  json.dumps(output, ensure_ascii=False, sort_keys=True)
                  if output is not None else None, core.now(), job_id)
        if owner is not None:
            values += (owner,)
        changed = conn.execute(
            'UPDATE jobs SET status=?,error_class=?,error_detail=?,block_reason=?,'
            f'output_json=?,updated=? WHERE id=?{clause}', values).rowcount
        if changed != 1:
            raise LeaseLost('Worker no longer owns the job')
        _append(conn, job_id, 'status', {'status': status,
                                         'block_reason': block_reason,
                                         'error_class': error_class})
        return _row(conn, job_id)


def pause(job_id: str, db=None) -> dict:
    with _tx(db) as conn:
        job = _row(conn, job_id)
        if job['status'] in ('PENDING', 'PAUSED'):
            conn.execute('UPDATE jobs SET status=?,updated=? WHERE id=?',
                         ('PAUSED', core.now(), job_id))
            _append(conn, job_id, 'paused', {})
        elif job['status'] == 'RUNNING':
            conn.execute('UPDATE jobs SET pause_requested=1,updated=? WHERE id=?',
                         (core.now(), job_id))
            _append(conn, job_id, 'pause_requested', {})
        else:
            raise ValueError('Job cannot be paused')
        return _row(conn, job_id)


def resume(job_id: str, db=None) -> dict:
    with _tx(db) as conn:
        job = _row(conn, job_id)
        if job['status'] != 'PAUSED':
            raise ValueError('Job is not paused')
        conn.execute(
            "UPDATE jobs SET status='PENDING',pause_requested=0,updated=? WHERE id=?",
            (core.now(), job_id))
        _append(conn, job_id, 'resumed', {})
        return _row(conn, job_id)


def cancel(job_id: str, db=None) -> dict:
    with _tx(db) as conn:
        job = _row(conn, job_id)
        if job['status'] == 'CANCELLED':
            return job
        if job['status'] in ('PENDING', 'PAUSED'):
            conn.execute('UPDATE jobs SET status=?,cancel_requested=1,updated=? WHERE id=?',
                         ('CANCELLED', core.now(), job_id))
            _append(conn, job_id, 'cancelled', {})
        elif job['status'] == 'RUNNING':
            conn.execute('UPDATE jobs SET cancel_requested=1,updated=? WHERE id=?',
                         (core.now(), job_id))
            _append(conn, job_id, 'cancel_requested',
                    {'warning': 'remote may continue and bill'})
        else:
            raise ValueError('Job cannot be cancelled')
        return _row(conn, job_id)


def stop_requested(job_id: str, db=None) -> str | None:
    job = get(job_id, db)
    if job['cancel_requested']:
        return 'cancel'
    if job['pause_requested']:
        return 'pause'
    return None


def _future(now: str, seconds: int) -> tuple[str, str]:
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat(), (current + timedelta(seconds=seconds)).isoformat()


def claim(owner: str, *, lease_seconds: int = 60, now: str | None = None,
          db=None) -> dict | None:
    if not isinstance(owner, str) or not owner:
        raise ValueError('Worker owner is required')
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError('Invalid lease duration')
    current, expiry = _future(now, lease_seconds)
    with _tx(db) as conn:
        flagged = conn.execute(
            "SELECT id,cancel_requested,pause_requested FROM jobs WHERE status='RUNNING' "
            'AND lease_expires IS NOT NULL AND lease_expires <= ? '
            'AND (cancel_requested=1 OR pause_requested=1)', (current,)).fetchall()
        for item in flagged:
            target = 'CANCELLED' if item['cancel_requested'] else 'PAUSED'
            conn.execute(
                'UPDATE jobs SET status=?,lease_owner=NULL,lease_expires=NULL,updated=? '
                'WHERE id=?', (target, current, item['id']))
            _append(conn, item['id'], 'stopped_before_claim', {'status': target})
        row = conn.execute(
            "SELECT * FROM jobs WHERE (status='PENDING' OR "
            "(status='RUNNING' AND lease_expires IS NOT NULL AND lease_expires <= ?)) "
            'AND cancel_requested=0 AND pause_requested=0 '
            'ORDER BY priority DESC, created, id LIMIT 1', (current,)).fetchone()
        if row is None:
            return None
        recovered = row['status'] == 'RUNNING'
        changed = conn.execute(
            "UPDATE jobs SET status='RUNNING',lease_owner=?,lease_expires=?,updated=? "
            "WHERE id=? AND (status='PENDING' OR status='RUNNING')",
            (owner, expiry, current, row['id'])).rowcount
        if changed != 1:
            return None
        _append(conn, row['id'], 'claimed',
                {'owner': owner, 'recovered': recovered})
        job = _row(conn, row['id'])
        job['recovered'] = recovered
        return job


def heartbeat(job_id: str, owner: str, *, lease_seconds: int = 60,
              now: str | None = None, db=None) -> bool:
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError('Invalid lease duration')
    current, expiry = _future(now, lease_seconds)
    conn = db or core.connect()
    try:
        changed = conn.execute(
            "UPDATE jobs SET lease_expires=?,updated=? WHERE id=? AND lease_owner=? "
            "AND status='RUNNING'", (expiry, current, job_id, owner)).rowcount
        if db is None:
            conn.commit()
        return changed == 1
    finally:
        if db is None:
            conn.close()


def release(job_id: str, owner: str, db=None) -> None:
    with _tx(db) as conn:
        changed = conn.execute(
            "UPDATE jobs SET lease_owner=NULL,lease_expires=NULL,updated=? "
            "WHERE id=? AND lease_owner=?", (core.now(), job_id, owner)).rowcount
        if changed == 1:
            _append(conn, job_id, 'lease_released', {'owner': owner})


def recovery_action(job_id: str, *, max_attempts: int = 2, db=None) -> str:
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError('Invalid max attempts')
    attempt = latest_attempt(job_id, db)
    if attempt is None:
        return 'execute'
    status = attempt['status']
    if status == 'COMPLETED':
        return 'complete'
    if status == 'SUBMITTING':
        return 'block_unknown'
    if status == 'UNKNOWN_REMOTE_STATE':
        return 'resume' if attempt['remote_id'] else 'block_unknown'
    if status in ('SUBMITTED', 'POLLING', 'DOWNLOADING', 'VALIDATING'):
        return 'resume' if attempt['remote_id'] else 'block_unknown'
    if status == 'FAILED_RETRYABLE':
        return 'execute' if attempt['attempt_no'] < max_attempts else 'fail'
    if status in ('FAILED_FINAL', 'ABANDONED'):
        return 'fail'
    return 'execute'
