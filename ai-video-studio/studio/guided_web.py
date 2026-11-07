"""FastAPI adapter for the opt-in guided workflow."""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from . import core, guided_generation, guided_storyboard, guided_workflow

router = APIRouter(prefix='/api/projects/{pid}/guided')


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


class Plan(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    shot_length: int | None = None
    budget_usd: float = Field(default=0, ge=0, le=10000)
    confirm_paid: bool = False
    revision: int = Field(default=0, ge=0)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class Revision(BaseModel):
    revision: int = Field(ge=0)


class Draft(Revision):
    draft: dict


class Generate(BaseModel):
    budget_usd: float = Field(default=0, ge=0, le=10000)
    confirm_paid: bool = False
    request_id: str = Field(min_length=1, max_length=128)


class Confirm(Revision):
    asset_id: str = Field(min_length=1)


class ShotEdit(Revision):
    prompt: str = Field(min_length=1, max_length=10000)


class Recover(BaseModel):
    remote_id: str | None = None


@router.get('/')
def get(pid: str):
    return call(guided_workflow.get, pid)


@router.post('/plan/preview')
def plan_preview(pid: str, body: Plan):
    call(guided_workflow.get, pid)
    return call(guided_storyboard.preview, pid, body.model, body.shot_length)


@router.post('/plan')
def plan(pid: str, body: Plan):
    if not body.confirm_paid or not body.budget_usd > 0 or not body.request_id:
        raise HTTPException(400, 'Confirmation, positive budget and request ID required')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = guided_workflow._row(db, pid)
        existing = db.execute('SELECT * FROM guided_planning_attempts WHERE project_id=? AND request_id=?',
                              (pid, body.request_id)).fetchone()
        if existing:
            if existing['status'] == 'COMPLETED':
                return guided_workflow.get(pid)
            raise HTTPException(400, 'Planning request is unresolved; do not resubmit')
        if db.execute("SELECT 1 FROM guided_planning_attempts WHERE project_id=? AND status IN ('SUBMITTING','UNKNOWN')", (pid,)).fetchone():
            raise HTTPException(400, 'Earlier planning request is unresolved')
        if row['revision'] != body.revision or row['storyboard_confirmed']:
            raise HTTPException(400, 'Stale revision or storyboard already confirmed')
        db.execute('INSERT INTO guided_planning_attempts VALUES (?,?,?,?,?,?,?)',
                   (pid, body.request_id, 'SUBMITTING', body.model, body.budget_usd, None, None))
    try:
        draft = guided_storyboard.request_plan(pid, body.model, body.budget_usd,
                                               confirm_paid=True, shot_length=body.shot_length)
        with core.connect() as db:
            db.execute('UPDATE guided_planning_attempts SET result_json=? WHERE project_id=? AND request_id=?',
                       (json.dumps(draft, ensure_ascii=False), pid, body.request_id))
        state = guided_workflow.save_draft(pid, draft, body.revision)
        with core.connect() as db:
            db.execute("UPDATE guided_planning_attempts SET status='COMPLETED' WHERE project_id=? AND request_id=?", (pid, body.request_id))
        return state
    except Exception as exc:
        with core.connect() as db:
            db.execute("UPDATE guided_planning_attempts SET status='UNKNOWN',error=? WHERE project_id=? AND request_id=?",
                       (str(exc)[:500], pid, body.request_id))
        raise HTTPException(400, 'Planning failed or outcome is uncertain; check charges before resolving: '+str(exc)) from exc


@router.get('/plan/attempts')
def plan_attempts(pid: str):
    call(guided_workflow.get, pid)
    with core.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM guided_planning_attempts WHERE project_id=? ORDER BY rowid DESC', (pid,))]


@router.post('/plan/attempts/{request_id}/resolve')
def resolve_plan(pid: str, request_id: str):
    call(guided_workflow.get, pid)
    with core.connect() as db:
        result = db.execute("UPDATE guided_planning_attempts SET status='ACKNOWLEDGED' WHERE project_id=? AND request_id=? AND status IN ('SUBMITTING','UNKNOWN')", (pid, request_id))
        if result.rowcount != 1:
            raise HTTPException(400, 'No unresolved planning attempt')
    return {'status': 'ACKNOWLEDGED'}


@router.put('/draft')
def draft(pid: str, body: Draft):
    return call(guided_workflow.save_draft, pid, body.draft, body.revision)


@router.post('/storyboard/confirm')
def storyboard_confirm(pid: str, body: Revision):
    return call(guided_workflow.confirm_storyboard, pid, body.revision)


@router.post('/shots/{seq}/preview')
def shot_preview(pid: str, seq: int):
    return call(guided_generation.preview, pid, seq)


@router.patch('/shots/{seq}')
def shot_edit(pid: str, seq: int, body: ShotEdit):
    return call(guided_workflow.edit_shot, pid, seq, body.prompt, body.revision)


@router.post('/shots/{seq}/generate')
def shot_generate(pid: str, seq: int, body: Generate):
    return call(guided_generation.submit, pid, seq, body.budget_usd,
                confirm_paid=body.confirm_paid, request_id=body.request_id)


@router.post('/shots/{seq}/confirm')
def shot_confirm(pid: str, seq: int, body: Confirm):
    return call(guided_workflow.confirm_shot, pid, seq, body.asset_id, body.revision)


@router.get('/attempts')
def attempts(pid: str):
    call(guided_workflow.get, pid)
    with core.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM guided_attempts WHERE project_id=? ORDER BY rowid DESC', (pid,))]


@router.get('/attempts/{attempt_id}')
def attempt(pid: str, attempt_id: str):
    return call(guided_generation.attempt, pid, attempt_id)


@router.post('/attempts/{attempt_id}/recover')
def recover(pid: str, attempt_id: str, body: Recover):
    return call(guided_generation.recover, pid, attempt_id, body.remote_id)


@router.post('/render', status_code=202)
def render(pid: str, body: Revision):
    from .webapp import start_render, RenderRequest
    state = call(guided_workflow.get, pid)
    if state['revision'] != body.revision or not call(guided_workflow.render_ready, pid):
        raise HTTPException(400, 'All shots must be confirmed at the current revision')
    return start_render(pid, RenderRequest(source='selected', silent_fallback=True))
