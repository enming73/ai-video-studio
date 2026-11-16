"""Attempt state machine. Reservation and submission intent share one transaction."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

from . import core, jobs, ledger

ACTIVE = ('PLANNED', 'RESERVED', 'SUBMITTING', 'SUBMITTED',
          'POLLING', 'DOWNLOADING', 'VALIDATING')
TERMINAL = ('COMPLETED', 'FAILED_RETRYABLE', 'FAILED_FINAL',
            'UNKNOWN_REMOTE_STATE', 'ABANDONED')
ALLOWED = {
    'PLANNED': {'RESERVED', 'ABANDONED'},
    'RESERVED': {'SUBMITTING', 'ABANDONED'},
    'SUBMITTING': {'SUBMITTED', 'COMPLETED', 'FAILED_RETRYABLE', 'FAILED_FINAL',
                   'UNKNOWN_REMOTE_STATE', 'ABANDONED'},
    'SUBMITTED': {'POLLING', 'UNKNOWN_REMOTE_STATE', 'FAILED_FINAL', 'ABANDONED'},
    'POLLING': {'DOWNLOADING', 'UNKNOWN_REMOTE_STATE', 'FAILED_FINAL', 'ABANDONED'},
    'DOWNLOADING': {'VALIDATING', 'UNKNOWN_REMOTE_STATE', 'FAILED_FINAL', 'ABANDONED'},
    'VALIDATING': {'COMPLETED', 'UNKNOWN_REMOTE_STATE', 'FAILED_FINAL', 'ABANDONED'},
    'UNKNOWN_REMOTE_STATE': {'SUBMITTED', 'ABANDONED'},
}


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


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _row(conn, attempt_id: str) -> dict:
    row = conn.execute('SELECT * FROM job_attempts WHERE id=?', (attempt_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown attempt')
    return dict(row)


def get(attempt_id: str, db=None) -> dict:
    conn = db or core.connect()
    try:
        return _row(conn, attempt_id)
    finally:
        if db is None:
            conn.close()


def list_for_job(job_id: str, db=None) -> list[dict]:
    conn = db or core.connect()
    try:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_no', (job_id,))]
    finally:
        if db is None:
            conn.close()


def create(job_id: str, *, provider: str, model: str, input_data: dict,
           db=None) -> dict:
    if not all(isinstance(value, str) and value for value in (provider, model)):
        raise ValueError('Provider and model are required')
    fingerprint = hashlib.sha256(_canonical(input_data).encode()).hexdigest()
    placeholders = ','.join('?' for _ in ACTIVE)
    with _tx(db) as conn:
        job = jobs.get(job_id, db=conn)
        existing = conn.execute(
            f'SELECT * FROM job_attempts WHERE job_id=? AND request_fingerprint=? '
            f'AND status IN ({placeholders}) ORDER BY attempt_no DESC LIMIT 1',
            (job_id, fingerprint, *ACTIVE)).fetchone()
        if existing is not None:
            return dict(existing)
        attempt_no = conn.execute(
            'SELECT COALESCE(MAX(attempt_no),0)+1 FROM job_attempts WHERE job_id=?',
            (job_id,)).fetchone()[0]
        idempotency_key = hashlib.sha256(
            f'{job_id}:{attempt_no}:{job["run_id"]}'.encode()).hexdigest()
        attempt_id = hashlib.sha256(f'{idempotency_key}:attempt'.encode()).hexdigest()[:32]
        now = core.now()
        conn.execute(
            'INSERT INTO job_attempts(id,job_id,attempt_no,provider,model,status,'
            'request_fingerprint,idempotency_key,input_json,created,updated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (attempt_id, job_id, attempt_no, provider, model, 'PLANNED',
             fingerprint, idempotency_key, _canonical(input_data), now, now))
        jobs.append_event(job_id, 'attempt_created',
                          {'attempt_no': attempt_no, 'provider': provider},
                          attempt_id=attempt_id, db=conn)
        return _row(conn, attempt_id)


def _transition(conn, attempt_id: str, status: str, fields: dict) -> dict:
    current = _row(conn, attempt_id)
    if status not in ALLOWED.get(current['status'], set()):
        raise ValueError(f"Illegal attempt transition {current['status']} -> {status}")
    assignments = ['status=?', 'updated=?']
    values = [status, core.now()]
    for key in ('remote_id', 'error_class', 'error_detail', 'output_json',
                'retry_after_sec'):
        if key in fields:
            assignments.append(f'{key}=?')
            values.append(fields[key])
    values.append(attempt_id)
    conn.execute(f'UPDATE job_attempts SET {",".join(assignments)} WHERE id=?', values)
    jobs.append_event(current['job_id'], 'attempt_status',
                      {'status': status, **fields}, attempt_id=attempt_id, db=conn)
    return _row(conn, attempt_id)


def transition(attempt_id: str, status: str, *, db=None, **fields) -> dict:
    with _tx(db) as conn:
        return _transition(conn, attempt_id, status, fields)


def prepare_submission(attempt_id: str, *, amount_microusd: int,
                       pricing_version: str, evidence: str | None = None,
                       db=None) -> str:
    """Reserve funds and persist SUBMITTING atomically, before any network call."""
    with _tx(db) as conn:
        attempt = _row(conn, attempt_id)
        if attempt['status'] != 'PLANNED':
            raise ValueError('Attempt is not ready for submission')
        job = jobs.get(attempt['job_id'], db=conn)
        _transition(conn, attempt_id, 'RESERVED', {})
        entry_id = ledger.reserve(
            job['project_id'], stage=job['kind'], provider=attempt['provider'],
            model=attempt['model'], amount_microusd=amount_microusd,
            pricing_version=pricing_version, run_id=job['run_id'],
            job_id=job['id'], attempt_id=attempt_id, estimated_microusd=amount_microusd,
            evidence=evidence, db=conn)
        _transition(conn, attempt_id, 'SUBMITTING', {})
        return entry_id


def attach_remote(attempt_id: str, remote_id: str, db=None) -> dict:
    if not isinstance(remote_id, str) or not remote_id.strip():
        raise ValueError('Remote id is required')
    with _tx(db) as conn:
        attempt = _row(conn, attempt_id)
        if attempt['status'] not in ('SUBMITTING', 'UNKNOWN_REMOTE_STATE'):
            raise ValueError('Attempt cannot attach a remote id')
        return _transition(conn, attempt_id, 'SUBMITTED', {'remote_id': remote_id.strip()})


def mark_unknown(attempt_id: str, detail: str, db=None) -> dict:
    with _tx(db) as conn:
        return _transition(conn, attempt_id, 'UNKNOWN_REMOTE_STATE',
                           {'error_detail': str(detail)[:1200]})


def mark_failed(attempt_id: str, kind: str, detail: str, *, error_class: str | None = None,
                retry_after_sec: int | None = None, db=None) -> dict:
    if kind not in ('FAILED_RETRYABLE', 'FAILED_FINAL'):
        raise ValueError('Invalid failure kind')
    with _tx(db) as conn:
        return _transition(conn, attempt_id, kind,
                           {'error_detail': str(detail)[:1200],
                            'error_class': error_class,
                            'retry_after_sec': retry_after_sec})


def mark_status(attempt_id: str, status: str, db=None) -> dict:
    with _tx(db) as conn:
        return _transition(conn, attempt_id, status, {})


def mark_completed(attempt_id: str, output: dict, db=None) -> dict:
    with _tx(db) as conn:
        return _transition(conn, attempt_id, 'COMPLETED',
                           {'output_json': _canonical(output)})


def abandon(attempt_id: str, evidence: str, db=None) -> dict:
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('Abandon evidence is required')
    with _tx(db) as conn:
        return _transition(conn, attempt_id, 'ABANDONED', {'error_detail': evidence})
