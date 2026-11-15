"""Atomic, integer micro-USD cost ledger. Not a provider invoice."""
from __future__ import annotations

import math
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from . import core

MICRO_PER_USD = 1_000_000


class BudgetExceeded(ValueError):
    def __init__(self, scope: str, committed_microusd: int,
                 limit_microusd: int, requested_microusd: int):
        self.scope = scope
        self.committed_microusd = committed_microusd
        self.limit_microusd = limit_microusd
        self.requested_microusd = requested_microusd
        super().__init__(
            f'{scope} budget exceeded: committed {committed_microusd} + '
            f'requested {requested_microusd} > limit {limit_microusd}')


def to_micro(usd) -> int:
    if isinstance(usd, bool):
        raise ValueError('Money cannot be a boolean')
    try:
        value = Decimal(str(usd))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid money value') from exc
    if not value.is_finite() or value < 0:
        raise ValueError('Money must be finite and nonnegative')
    return int((value * MICRO_PER_USD).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def to_usd(micro: int) -> float:
    if isinstance(micro, bool) or not isinstance(micro, int) or micro < 0:
        raise ValueError('Micro-USD must be a nonnegative integer')
    return micro / MICRO_PER_USD


@contextmanager
def _transaction(db=None):
    owned = db is None
    connection = core.connect() if owned else db
    try:
        if owned:
            connection.execute('BEGIN IMMEDIATE')
        yield connection
        if owned:
            connection.commit()
    except Exception:
        if owned:
            connection.rollback()
        raise
    finally:
        if owned:
            connection.close()


def _now() -> str:
    return core.now()


def open_run(pid: str, budget_microusd: int | None = None,
             label: str = '', db=None) -> str:
    if budget_microusd is not None:
        if (isinstance(budget_microusd, bool) or not isinstance(budget_microusd, int)
                or budget_microusd < 0):
            raise ValueError('Run budget must be a nonnegative integer')
    with _transaction(db) as conn:
        core.project(conn, pid)
        run_id = uuid4().hex
        conn.execute(
            'INSERT INTO runs(id,project_id,status,budget_limit_microusd,label,created) '
            'VALUES (?,?,?,?,?,?)',
            (run_id, pid, 'OPEN', budget_microusd, label, _now()))
    return run_id


def close_run(run_id: str, status: str = 'CLOSED', db=None) -> None:
    if status not in ('CLOSED', 'CANCELLED'):
        raise ValueError('Invalid run close status')
    with _transaction(db) as conn:
        changed = conn.execute(
            "UPDATE runs SET status=?, closed=? WHERE id=? AND status='OPEN'",
            (status, _now(), run_id)).rowcount
        if changed != 1:
            raise ValueError('Run not found or already closed')


def _project_limit(conn, pid: str) -> int:
    return to_micro(core.project(conn, pid)['budget'])


def _run_limit(conn, run_id: str) -> int | None:
    row = conn.execute(
        'SELECT status,budget_limit_microusd FROM runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown run')
    if row['status'] != 'OPEN':
        raise ValueError('Run is not open')
    return row['budget_limit_microusd']


_COMMITTED_SQL = (
    "SELECT COALESCE(SUM(CASE WHEN billing_status='settled' THEN actual_microusd "
    "WHEN billing_status IN ('reserved','unknown') THEN reserved_microusd ELSE 0 END),0) "
    "FROM cost_entries WHERE project_id=?"
)


def _committed(conn, pid: str, run_id: str | None = None) -> int:
    if run_id is None:
        return int(conn.execute(_COMMITTED_SQL, (pid,)).fetchone()[0])
    return int(conn.execute(
        _COMMITTED_SQL + ' AND run_id=?', (pid, run_id)).fetchone()[0])


def reserve(pid: str, *, stage: str, provider: str, model: str,
            amount_microusd: int, pricing_version: str,
            run_id: str | None = None, job_id: str | None = None,
            attempt_id: str | None = None, estimated_microusd: int | None = None,
            evidence: str | None = None, db=None) -> str:
    if (isinstance(amount_microusd, bool) or not isinstance(amount_microusd, int)
            or amount_microusd <= 0):
        raise ValueError('Reservation must be a positive integer')
    if not isinstance(pricing_version, str) or not pricing_version.strip():
        raise ValueError('Pricing version is required')
    if not all(isinstance(value, str) and value for value in (stage, provider, model)):
        raise ValueError('Stage, provider and model are required')
    with _transaction(db) as conn:
        core.project(conn, pid)
        run_limit = _run_limit(conn, run_id) if run_id else None
        project_limit = _project_limit(conn, pid)
        project_committed = _committed(conn, pid)
        if project_committed + amount_microusd > project_limit:
            raise BudgetExceeded('project', project_committed,
                                 project_limit, amount_microusd)
        if run_id is not None and run_limit is not None:
            run_committed = _committed(conn, pid, run_id)
            if run_committed + amount_microusd > run_limit:
                raise BudgetExceeded('run', run_committed,
                                     run_limit, amount_microusd)
        entry_id = uuid4().hex
        conn.execute(
            'INSERT INTO cost_entries(id,project_id,run_id,job_id,attempt_id,stage,'
            'provider,model,pricing_version,reserved_microusd,estimated_microusd,'
            'billing_status,evidence,created,updated) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (entry_id, pid, run_id, job_id, attempt_id, stage, provider, model,
             pricing_version, amount_microusd, estimated_microusd, 'reserved',
             evidence, _now(), _now()))
    return entry_id


def committed(pid: str, run_id: str | None = None, db=None) -> int:
    conn = db or core.connect()
    try:
        return _committed(conn, pid, run_id)
    finally:
        if db is None:
            conn.close()


def _transition(conn, entry_id: str, allowed: tuple[str, ...], values: dict) -> None:
    assignments = ','.join(f'{key}=?' for key in values)
    placeholders = ','.join('?' for _ in allowed)
    changed = conn.execute(
        f'UPDATE cost_entries SET {assignments},updated=? '
        f'WHERE id=? AND billing_status IN ({placeholders})',
        (*values.values(), _now(), entry_id, *allowed)).rowcount
    if changed != 1:
        raise ValueError('Cost entry not transitionable')


def settle(entry_id: str, actual_microusd: int, evidence: str, db=None) -> None:
    if (isinstance(actual_microusd, bool) or not isinstance(actual_microusd, int)
            or actual_microusd < 0):
        raise ValueError('Actual amount must be a nonnegative integer')
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('Settlement evidence is required')
    with _transaction(db) as conn:
        _transition(conn, entry_id, ('reserved', 'unknown'), {
            'billing_status': 'settled',
            'actual_microusd': actual_microusd,
            'evidence': evidence,
        })


def mark_unknown(entry_id: str, evidence: str | None = None, db=None) -> None:
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        raise ValueError('Evidence must be nonempty when provided')
    with _transaction(db) as conn:
        values = {'billing_status': 'unknown'}
        if evidence is not None:
            values['evidence'] = evidence
        _transition(conn, entry_id, ('reserved',), values)


def release(entry_id: str, evidence: str, db=None) -> None:
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('Release evidence is required')
    with _transaction(db) as conn:
        _transition(conn, entry_id, ('reserved', 'unknown'), {
            'billing_status': 'released', 'evidence': evidence})


def summary(pid: str, run_id: str | None = None, db=None) -> dict:
    conn = db or core.connect()
    try:
        sql = ('SELECT stage,billing_status,COUNT(*) AS count,'
               'COALESCE(SUM(CASE WHEN billing_status=\'settled\' THEN actual_microusd '
               'ELSE 0 END),0) AS settled,'
               'COALESCE(SUM(CASE WHEN billing_status IN (\'reserved\',\'unknown\') '
               'THEN reserved_microusd ELSE 0 END),0) AS open,'
               'COALESCE(SUM(CASE WHEN billing_status=\'unknown\' THEN 1 ELSE 0 END),0) '
               'AS unknown_count FROM cost_entries WHERE project_id=?')
        args = [pid]
        if run_id is not None:
            sql += ' AND run_id=?'
            args.append(run_id)
        rows = conn.execute(sql + ' GROUP BY stage,billing_status', args).fetchall()
        by_stage: dict[str, dict] = {}
        settled = open_exposure = unknown_count = 0
        for row in rows:
            stage = by_stage.setdefault(row['stage'], {
                'settled_microusd': 0, 'open_exposure_microusd': 0,
                'committed_microusd': 0, 'unknown_count': 0})
            stage['settled_microusd'] += int(row['settled'])
            stage['open_exposure_microusd'] += int(row['open'])
            stage['committed_microusd'] += int(row['settled']) + int(row['open'])
            stage['unknown_count'] += int(row['unknown_count'])
            settled += int(row['settled'])
            open_exposure += int(row['open'])
            unknown_count += int(row['unknown_count'])
        return {
            'settled_microusd': settled,
            'open_exposure_microusd': open_exposure,
            'committed_microusd': settled + open_exposure,
            'unknown_count': unknown_count,
            'by_stage': by_stage,
        }
    finally:
        if db is None:
            conn.close()

