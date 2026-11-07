"""One immutable output and one durable submission record per guided attempt."""
import math
import os
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from . import core, timeline, guided_workflow, guided_storyboard, provider_capabilities

_RATES = {'minimax': 'MINIMAX_USD_PER_SECOND', 'wan': 'WAN_USD_PER_SECOND', 'flux': 'FAL_USD_PER_SECOND'}
_ACTIVE = ('SUBMITTING', 'SUBMITTED', 'RUNNING', 'DOWNLOAD_PENDING')


def _shot(db, pid, seq):
    workflow = guided_workflow._row(db, pid)
    if not workflow['storyboard_confirmed']:
        raise ValueError('Confirm storyboard first')
    shot = db.execute('SELECT * FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
    if not shot:
        raise ValueError('Unknown shot')
    missing = db.execute('''SELECT COUNT(*) FROM shots s LEFT JOIN guided_confirmations c
        ON c.project_id=s.project_id AND c.shot_id=s.id WHERE s.project_id=? AND s.seq<?
        AND c.shot_id IS NULL''', (pid, seq)).fetchone()[0]
    if missing:
        raise ValueError('Earlier shots must be confirmed')
    return dict(shot)


def _rate(provider):
    load_dotenv()
    if provider == 'mock':
        return 0.0
    try:
        value = float(os.environ[_RATES[provider]])
    except (KeyError, ValueError):
        raise ValueError('Set a verified provider USD-per-second rate') from None
    if not math.isfinite(value) or not 0 < value < 100:
        raise ValueError('Invalid provider rate')
    return value


def preview(pid: str, seq: int) -> dict:
    with core.connect() as db:
        p = dict(core.project(db, pid))
        shot = _shot(db, pid, seq)
    cap = provider_capabilities.capabilities(p['provider'], guided_storyboard._MODELS[p['provider']])
    if not cap['verified'] or shot['duration'] not in cap['durations']:
        raise ValueError('Provider duration capability unverified or unsupported')
    rate = _rate(p['provider'])
    return {'project_id': pid, 'seq': seq, 'duration': shot['duration'], 'prompt': shot['prompt'],
            'paid': p['provider'] != 'mock', 'estimated_cost_usd': round(rate * shot['duration'], 6),
            'note': 'Estimated reservation is not a provider-enforced spending cap.'}


def attempt(pid: str, attempt_id: str) -> dict:
    with core.connect() as db:
        guided_workflow._row(db, pid)
        row = db.execute('SELECT * FROM guided_attempts WHERE project_id=? AND id=?', (pid, attempt_id)).fetchone()
        if row is None:
            raise ValueError('Unknown attempt')
        return dict(row)


def submit(pid: str, seq: int, budget_usd: float, *, confirm_paid: bool, request_id: str) -> dict:
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    estimate = preview(pid, seq)
    if estimate['paid'] and (confirm_paid is not True or isinstance(budget_usd, bool)
            or not isinstance(budget_usd, (int, float)) or not math.isfinite(budget_usd)
            or budget_usd <= 0 or budget_usd < estimate['estimated_cost_usd']):
        raise ValueError('Explicit confirmation and sufficient positive budget required')
    aid = uuid4().hex
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = dict(core.project(db, pid))
        shot = _shot(db, pid, seq)
        existing = db.execute('SELECT * FROM guided_attempts WHERE project_id=? AND request_id=?', (pid, request_id)).fetchone()
        if existing:
            if existing['shot_id'] != shot['id']:
                raise ValueError('Request id belongs to another shot')
            return dict(existing)
        active = db.execute("SELECT 1 FROM guided_attempts WHERE project_id=? AND shot_id=? AND status IN ('SUBMITTING','SUBMITTED','RUNNING','DOWNLOAD_PENDING')", (pid, shot['id'])).fetchone()
        if active:
            raise ValueError('An earlier attempt is unresolved; recover it before submitting again')
        if shot['prompt'] != estimate['prompt'] or shot['duration'] != estimate['duration']:
            raise ValueError('Shot changed after preview')
        reserved = db.execute('SELECT COALESCE(SUM(reserved_usd),0) FROM guided_attempts WHERE project_id=?', (pid,)).fetchone()[0]
        if estimate['paid'] and reserved + estimate['estimated_cost_usd'] > p['budget'] + 1e-9:
            raise ValueError('Project budget insufficient')
        db.execute('''INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status)
                      VALUES (?,?,?,?,?,?,?,?,?)''', (aid, pid, shot['id'], request_id, shot['prompt'], shot['duration'],
                                                    p['provider'], estimate['estimated_cost_usd'], 'SUBMITTING'))
    if p['provider'] == 'mock':
        try:
            directory = core.HOME / pid / 'guided-attempts' / aid
            directory.mkdir(parents=True, exist_ok=False)
            target = directory / 'video.mp4'
            core.mock_generate(shot, target)
            core.verify_video(target, shot['duration'])
            _register(pid, aid, seq, target)
        except Exception as exc:
            _update(pid, aid, status='FAILED', error=str(exc)[:500])
        return attempt(pid, aid)
    try:
        with httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False) as client:
            remote = core.PROVIDERS[p['provider']](client).submit(shot['prompt'], shot['duration'])
        _update(pid, aid, status='SUBMITTED', remote_id=remote)
    except Exception as exc:
        _update(pid, aid, error=str(exc)[:500])
    return attempt(pid, aid)


def _update(pid, aid, **values):
    allowed = {'status', 'remote_id', 'video_url', 'file', 'asset_id', 'error'}
    if not values or not values.keys() <= allowed:
        raise ValueError('Invalid attempt update')
    with core.connect() as db:
        db.execute('UPDATE guided_attempts SET ' + ','.join(k+'=?' for k in values) + " WHERE project_id=? AND id=? AND status!='COMPLETED'",
                   (*values.values(), pid, aid))


def _register(pid, aid, seq, target):
    asset = timeline.import_asset(pid, seq, str(target), source='guided:' + aid)
    _update(pid, aid, status='COMPLETED', file=str(target), asset_id=asset, error=None)


def recover(pid: str, attempt_id: str, remote_id: str | None = None) -> dict:
    """Attach a user-verified ID or poll an existing task once; never submit."""
    state = attempt(pid, attempt_id)
    if state['status'] not in _ACTIVE:
        return state
    if remote_id is not None:
        if not isinstance(remote_id, str) or not remote_id.strip() or state['remote_id']:
            raise ValueError('Invalid or already-attached remote id')
        _update(pid, attempt_id, remote_id=remote_id.strip(), status='SUBMITTED')
        state = attempt(pid, attempt_id)
    if not state['remote_id']:
        raise ValueError('Remote task ID required; check provider dashboard before attaching')
    try:
        with httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False) as client:
            provider = core.PROVIDERS[state['provider']](client)
            url = state['video_url']
            if not url:
                result = provider.poll(state['remote_id'])
                if result.status == 'failed':
                    _update(pid, attempt_id, status='FAILED', error=result.error)
                    return attempt(pid, attempt_id)
                if result.status != 'succeeded':
                    _update(pid, attempt_id, status='RUNNING')
                    return attempt(pid, attempt_id)
                url = result.url
                _update(pid, attempt_id, status='DOWNLOAD_PENDING', video_url=url)
            from video_provider_core.media import download
            directory = core.HOME / pid / 'guided-attempts' / attempt_id
            directory.mkdir(parents=True, exist_ok=True)
            # A unique destination per download avoids overwrites even if two
            # recovery requests race. Existing files are never reused or removed.
            target = directory / (uuid4().hex + '.mp4')
            download(client, url, target)
            core.verify_video(target, state['duration'])
            with core.connect() as db:
                shot = db.execute('SELECT seq FROM shots WHERE project_id=? AND id=?', (pid, state['shot_id'])).fetchone()
            _register(pid, attempt_id, shot['seq'], target)
    except Exception as exc:
        _update(pid, attempt_id, error=str(exc)[:500])
    return attempt(pid, attempt_id)
