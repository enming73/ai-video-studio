"""Create persistent video jobs from legacy project shots and reconcile outputs."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from . import core, jobs, ledger, timeline

TERMINAL = jobs.TERMINAL
UNRESOLVED = ('PENDING', 'RUNNING', 'PAUSED', 'BLOCKED')


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


@contextmanager
def _conn(db=None):
    owned = db is None
    conn = core.connect() if owned else db
    try:
        yield conn
    finally:
        if owned:
            conn.close()


def open_or_reuse_run(pid: str, *, request_key: str, budget_microusd: int,
                      label: str = '', db=None) -> str:
    if not isinstance(request_key, str) or not request_key:
        raise ValueError('Run request key is required')
    if isinstance(budget_microusd, bool) or not isinstance(budget_microusd, int) \
            or budget_microusd <= 0:
        raise ValueError('Run budget must be a positive integer')
    with _tx(db) as conn:
        core.project(conn, pid)
        row = conn.execute(
            'SELECT id,status FROM runs WHERE project_id=? AND request_key=?',
            (pid, request_key)).fetchone()
        if row is not None:
            if row['status'] != 'OPEN':
                raise ValueError('Run is not open')
            return row['id']
        run_id = ledger.open_run(pid, budget_microusd, label=label, db=conn)
        conn.execute('UPDATE runs SET request_key=? WHERE id=?', (request_key, run_id))
        return run_id


def enqueue_shot(pid: str, seq: int, *, run_id: str, provider: str, model: str,
                 request_key: str, max_attempts: int = 2, db=None) -> dict:
    from .guided_storyboard import _MODELS
    with _tx(db) as conn:
        existing = conn.execute(
            'SELECT * FROM jobs WHERE project_id=? AND request_key=?',
            (pid, request_key)).fetchone()
        if existing is not None:
            return dict(existing)
        shot = conn.execute('SELECT * FROM shots WHERE project_id=? AND seq=?',
                            (pid, seq)).fetchone()
        if shot is None:
            raise ValueError('Unknown shot')
        active = conn.execute(
            'SELECT 1 FROM jobs WHERE project_id=? AND target_key=? '
            f'AND status IN ({",".join("?" for _ in UNRESOLVED)}) LIMIT 1',
            (pid, f'shot:{shot["id"]}', *UNRESOLVED)).fetchone()
        if active:
            return dict(conn.execute('SELECT * FROM jobs WHERE project_id=? AND target_key=? ORDER BY created DESC LIMIT 1', (pid, f'shot:{shot["id"]}')).fetchone())
        payload = {
            'prompt': shot['prompt'], 'duration_sec': shot['duration'],
            'width': 1280, 'height': 720, 'provider': provider, 'model': model,
            'poll_interval_sec': 5, 'max_attempts': max_attempts,
            'shot_id': shot['id'], 'seq': seq,
        }
        return jobs.create(pid, kind='video', payload=payload, request_key=request_key,
                           run_id=run_id, target_key=f'shot:{shot["id"]}', db=conn)




def enqueue_project(pid: str, *, budget_usd: float, request_key: str, limit: int = 0,
                    db=None) -> dict:
    if type(limit) is not int or limit < 0:
        raise ValueError('Invalid limit')
    budget = ledger.to_micro(budget_usd)
    from .guided_storyboard import _MODELS
    with _tx(db) as conn:
        project = core.project(conn, pid)
        provider = project['provider']
        if provider == 'mock':
            raise ValueError('Mock projects use local generation')
        run_id = open_or_reuse_run(pid, request_key=request_key,
                                   budget_microusd=budget, label=request_key, db=conn)
        blocked = ('SUBMITTING', 'GENERATING', 'SUBMITTED', 'RUNNING', 'DOWNLOAD_PENDING')
        placeholders = ','.join('?' for _ in blocked)
        shots = [dict(row) for row in conn.execute(
            f"SELECT * FROM shots WHERE project_id=? AND status NOT IN ('COMPLETED',{placeholders}) ORDER BY seq",
            (pid, *blocked))]
        skipped = [dict(row) for row in conn.execute(
            f'SELECT id,seq,status FROM shots WHERE project_id=? AND status IN ({placeholders})',
            (pid, *blocked))]
        if limit:
            shots = shots[:limit]
        created = []
        for shot in shots:
            created.append(enqueue_shot(
                pid, shot['seq'], run_id=run_id, provider=provider,
                model=_MODELS[provider], request_key=f'{request_key}:{shot["id"]}',
                db=conn))
        return {'run_id': run_id, 'jobs': created, 'skipped': skipped}


def run_until(job_ids: list[str], *, owner: str | None = None,
              timeout: float | None = None) -> list[dict]:
    import time
    from . import worker
    from .handlers import register_all
    if not worker.HANDLERS:
        register_all()
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        states = [jobs.get(job_id) for job_id in job_ids]
        if all(state['status'] in TERMINAL for state in states):
            return states
        if deadline is not None and time.monotonic() >= deadline:
            return states
        if worker.run_once(owner) is None:
            time.sleep(0.05)


def reconcile(pid: str, db=None) -> dict:
    updated = []
    for job in jobs.list_for_project(pid, limit=1000, db=db):
        if job['status'] != 'SUCCEEDED' or job['kind'] != 'video':
            continue
        payload = json.loads(job['payload_json'])
        output = json.loads(job['output_json'] or '{}')
        shot_id, seq, file = payload.get('shot_id'), payload.get('seq'), output.get('file')
        if not shot_id or not seq or not file or not Path(file).is_file():
            continue
        asset = timeline.import_asset(pid, seq, file, source='job:' + job['id'])
        with _tx(db) as conn:
            conn.execute("UPDATE shots SET status='COMPLETED',file=?,error=NULL WHERE id=?",
                         (file, shot_id))
        updated.append({'job_id': job['id'], 'seq': seq, 'asset_id': asset, 'file': file})
    return {'updated': updated}
