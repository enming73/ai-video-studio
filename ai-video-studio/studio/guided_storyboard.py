"""Budget-authorized storyboard proposals; never writes project shots."""
import json
import math
import os
from datetime import datetime, timezone
from uuid import uuid4

from . import core, provider_capabilities

_MODELS = {'mock': 'mock', 'minimax': 'MiniMax-H3', 'wan': 'wan3.0-video',
           'flux': 'blackforestlabs/flux-3-max/text-to-video'}


def preview(pid: str, model: str, shot_length: int | None = None) -> dict:
    if not isinstance(model, str) or not model.strip():
        raise ValueError('LLM model is required')
    with core.connect() as db:
        project = dict(core.project(db, pid))
        if db.execute('SELECT 1 FROM shots WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Existing storyboard cannot be replaced')
    capability = provider_capabilities.capabilities(project['provider'], _MODELS[project['provider']])
    if not capability['verified']:
        raise ValueError('Provider model duration capabilities are unverified')
    allowed = capability['durations']
    fixed = None
    if shot_length is not None:
        if type(shot_length) is not int or shot_length not in allowed:
            raise ValueError('Unsupported fixed shot length')
        full, remainder = divmod(project['duration'], shot_length)
        fixed = [shot_length] * full + ([remainder] if remainder else [])
        if not fixed or any(d not in allowed for d in fixed):
            raise ValueError('Final shot length is unsupported')
    if not provider_capabilities.feasible(project['duration'], allowed):
        raise ValueError('Total duration cannot be allocated with these shot lengths')
    return {'project_id': pid, 'model': model, 'duration': project['duration'],
            'allowed_durations': allowed, 'capability_source': capability['source'],
            'fixed_durations': fixed,
            'estimated_cost_usd': None, 'paid_request': False,
            'note': 'Reservation is not a provider-enforced spending cap.'}


def generate_draft(payload: dict) -> dict:
    """Pure LLM prompt construction and validation; no project writes."""
    prompt = {'brief': payload['brief'], 'total_seconds': payload['duration'],
              'allowed_shot_durations_seconds': payload['allowed_durations'],
              'required_shot_durations_seconds': payload['fixed_durations']}
    system = ('Create a coherent film storyboard. Return JSON with character_bible object and '
              'shots array of objects containing prompt and integer duration. Durations must '
              'be allowed and sum exactly to total_seconds. Character bible maps each character name to a description.')
    if payload.get('character_stage'):
        system += (' Every shot must include character_names as an array of exact character bible names, '
                   'or [] for no characters.')
    result = core._llm_json(
        system,
        json.dumps(prompt, ensure_ascii=False), payload['model'],
        payload.get('base_url') or os.getenv('STUDIO_LLM_BASE_URL', 'https://api.openai.com/v1'))
    if not isinstance(result, dict) or not isinstance(result.get('character_bible'), dict):
        raise ValueError('Invalid character bible')
    shots = result.get('shots')
    if not isinstance(shots, list) or any(not isinstance(s, dict) or
            not isinstance(s.get('prompt'), str) or len(s['prompt'].strip()) < 15 for s in shots):
        raise ValueError('Invalid storyboard shots')
    provider_capabilities.validate_durations(payload['duration'], [s.get('duration') for s in shots],
                                             payload['allowed_durations'])
    if payload['fixed_durations'] is not None and [s['duration'] for s in shots] != payload['fixed_durations']:
        raise ValueError('Storyboard does not match required fixed shot lengths')
    draft = {'character_bible': result['character_bible'], 'shots': [
        {'prompt': s['prompt'].strip(), 'duration': s['duration']} for s in shots]}
    if payload.get('character_stage'):
        roles = []
        names = {}
        for name, description in result['character_bible'].items():
            if not isinstance(name, str) or not name.strip() or name.strip().casefold() in names:
                raise ValueError('Invalid or duplicate character name')
            if isinstance(description, dict):
                description = json.dumps(description, ensure_ascii=False)
            if not isinstance(description, str) or not description.strip():
                raise ValueError('Invalid character description')
            cid = uuid4().hex
            names[name.strip().casefold()] = cid
            roles.append({'id': cid, 'name': name.strip(), 'description': description.strip()})
        for src, dst in zip(shots, draft['shots']):
            selected = src.get('character_names')
            if not isinstance(selected, list) or any(not isinstance(x, str) or x.casefold() not in names for x in selected):
                raise ValueError('Unknown or missing shot character list')
            ids = [names[x.casefold()] for x in selected]
            if len(set(ids)) != len(ids):
                raise ValueError('Duplicate shot character')
            dst['character_ids'] = ids
        draft['characters'] = roles
    return draft


def request_plan(pid: str, model: str, budget_usd: float, *, confirm_paid: bool,
                 shot_length: int | None = None, request_id: str, revision: int) -> dict:
    details = preview(pid, model, shot_length)
    if (confirm_paid is not True or isinstance(budget_usd, bool)
            or not isinstance(budget_usd, (float, int))
            or not math.isfinite(budget_usd) or budget_usd <= 0):
        raise ValueError('An explicit positive budget and paid confirmation are required')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    from . import guided_workflow
    with core.connect() as db:
        row = guided_workflow._row(db, pid)
        if row['revision'] != revision or row['storyboard_confirmed']:
            raise ValueError('Stale revision or storyboard already confirmed')
        project = dict(core.project(db, pid))
        character_stage = bool(db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone())
    payload = {
        'request_id': request_id,
        'brief': project['prompt'],
        'model': model,
        'duration': details['duration'],
        'allowed_durations': details['allowed_durations'],
        'fixed_durations': details['fixed_durations'],
        'character_stage': character_stage,
        'revision': revision,
        'budget_usd': budget_usd,
    }
    from . import guided_compat, request_jobs
    job = request_jobs.submit(pid, kind='llm_storyboard', request_id=request_id,
                              payload=payload, budget_usd=budget_usd, target_key='storyboard')
    return guided_compat.project_plan_job(job)


def _require_idle(job):
    if (job['status'] == 'RUNNING' and job['lease_owner'] and job['lease_expires']
            and datetime.fromisoformat(job['lease_expires']).replace(tzinfo=timezone.utc)
            > datetime.now(timezone.utc)):
        raise ValueError('An active worker owns this job; query it later')


def resolve_plan(pid: str, request_id: str, *, confirm_checked: bool) -> dict:
    """Acknowledge an unresolved planning attempt; a Job keeps its reservation."""
    if confirm_checked is not True:
        raise ValueError('Explicit confirmation is required: send confirm_checked=true')
    from . import guided_workflow, request_jobs, guided_compat, jobs
    from . import attempts as attempt_store
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._row(db, pid)
        row = db.execute('SELECT * FROM guided_planning_attempts WHERE project_id=? AND request_id=?',
                         (pid, request_id)).fetchone()
        if row:
            if row['status'] not in ('SUBMITTING', 'UNKNOWN'):
                raise ValueError('No unresolved planning attempt')
            db.execute("UPDATE guided_planning_attempts SET status='ACKNOWLEDGED' WHERE project_id=? AND request_id=?",
                       (pid, request_id))
            return {**dict(row), 'status': 'ACKNOWLEDGED'}
        job = request_jobs.find(pid, kind='llm_storyboard', request_id=request_id, db=db)
        if job is None:
            raise ValueError('Unknown planning attempt')
        _require_idle(job)
        current = jobs.latest_attempt(job['id'], db=db)
        if current and current['status'] == 'ABANDONED':
            return guided_compat.project_plan_job(job, db=db)
        if not current or current['status'] not in ('UNKNOWN_REMOTE_STATE', 'SUBMITTING'):
            raise ValueError('Only an unknown planning attempt can be resolved')
        attempt_store.abandon(current['id'], 'User checked; remote may continue and bill', db=db)
        db.execute("UPDATE jobs SET status='CANCELLED',cancel_requested=1,lease_owner=NULL,"
                   "lease_expires=NULL,updated=? WHERE id=?", (core.now(), job['id']))
        jobs.append_event(job['id'], 'guided_abandoned',
                          {'warning': 'remote may continue and bill'}, attempt_id=current['id'], db=db)
        return guided_compat.project_plan_job(jobs.get(job['id'], db=db), db=db)


def save_plan(pid: str, model: str, draft: dict) -> int:
    """Commit a validated CLI proposal atomically without replacing existing shots."""
    with core.connect() as db:
        project = dict(core.project(db, pid))
        if db.execute('SELECT 1 FROM guided_projects WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Use guided draft confirmation for this project')
        if db.execute('SELECT 1 FROM shots WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Existing storyboard cannot be replaced')
        capability = provider_capabilities.capabilities(project['provider'], _MODELS[project['provider']])
        if not capability['verified']:
            raise ValueError('Provider model duration capabilities are unverified')
        shots = draft['shots']
        provider_capabilities.validate_durations(project['duration'], [s['duration'] for s in shots],
                                                 capability['durations'])
        db.execute('INSERT INTO storyboards VALUES (?,?,?,?)',
                   (pid, json.dumps(draft['character_bible'], ensure_ascii=False), model, core.now()))
        for seq, shot in enumerate(shots, 1):
            db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)',
                       (f'{pid}-s{seq:03d}', pid, seq, shot['prompt'], shot['duration'], 'PLANNED'))
        db.execute("UPDATE projects SET status='PLANNED' WHERE id=?", (pid,))
    return len(shots)
