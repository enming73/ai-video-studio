"""FastAPI adapter for the opt-in guided workflow."""
import json
import os
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import core, guided_generation, guided_storyboard, guided_workflow
from . import guided_images, guided_characters, guided_boundaries, guided_structure

router = APIRouter(prefix='/api/projects/{pid}/guided')


@router.post('/characters/{character_id}/images')
async def upload_character_image(pid: str, character_id: str, revision: int = Form(...), file: UploadFile = File(...)):
    import tempfile
    from pathlib import Path as _Path
    suffix = _Path(file.filename or '').suffix.lower()
    if suffix not in guided_images._FORMATS:
        raise HTTPException(400, 'Unsupported image format')
    root = core.HOME / pid / 'guided-images' / '.incoming'
    root.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=root, suffix='.part')
    total = 0
    try:
        with os.fdopen(handle, 'wb') as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > guided_images.MAX_IMAGE_BYTES:
                    raise HTTPException(400, 'Image exceeds 20 MiB')
                out.write(chunk)
        return call(guided_images.import_character_image_file, pid, character_id, file.filename or '',
                    _Path(temp_name), revision)
    finally:
        _Path(temp_name).unlink(missing_ok=True)


@router.get('/images/{image_id}')
def get_character_image(pid: str, image_id: str):
    path = call(guided_images.image_path, pid, image_id)
    return FileResponse(path, headers={'Cache-Control': 'no-store'})


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


class CharacterEdit(Revision):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)


class ImageSelection(Revision):
    image_id: str = Field(min_length=1, max_length=128)


class CastSelection(Revision):
    character_ids: list[str]


class ImageRequest(Revision):
    model: str = 'gpt-image-2.5-sunburst'
    budget_usd: float = Field(default=0, ge=0, le=10000)
    confirm_paid: bool = False
    request_id: str = Field(default='', max_length=128)


class ImagePreview(BaseModel):
    model: str = 'gpt-image-2.5-sunburst'


class VideoModel(Revision):
    provider: str
    model: str


class ResolveImage(BaseModel):
    confirm_checked: bool = False


class Structure(Revision):
    shots: list[dict]


class BoundaryConfirm(Revision):
    allow_stale: bool = False


@router.post('/characters')
def create_character(pid: str, body: CharacterEdit):
    return call(guided_characters.save_character, pid, None, body.name, body.description, body.revision)


@router.get('/characters')
def characters(pid: str):
    return call(guided_characters.get, pid)


@router.post('/characters/activate')
def activate_characters(pid: str, body: Revision):
    return call(guided_characters.activate, pid, body.revision)


@router.post('/characters/empty-confirm')
def empty_cast(pid: str, body: Revision):
    return call(guided_characters.confirm_empty_cast, pid, body.revision)


@router.post('/characters/import-existing')
def import_existing_characters(pid: str, body: Revision):
    return call(guided_characters.import_existing_bible, pid, body.revision)


@router.patch('/characters/{character_id}')
def edit_character(pid: str, character_id: str, body: CharacterEdit):
    return call(guided_characters.save_character, pid, character_id, body.name, body.description, body.revision)


@router.get('/characters/{character_id}/images')
def character_images(pid: str, character_id: str):
    return call(guided_images.list_character_images, pid, character_id)


@router.post('/characters/{character_id}/select')
def select_character_image(pid: str, character_id: str, body: ImageSelection):
    return call(guided_characters.select_character_image, pid, character_id, body.image_id, body.revision)


@router.post('/characters/{character_id}/confirm')
def confirm_character(pid: str, character_id: str, body: ImageSelection):
    return call(guided_characters.confirm_character, pid, character_id, body.image_id, body.revision)


@router.post('/characters/{character_id}/images/preview')
def character_image_preview(pid: str, character_id: str, body: ImageRequest):
    return call(guided_images.preview_character, pid, character_id, body.model)


@router.post('/characters/{character_id}/images/generate')
def character_image_generate(pid: str, character_id: str, body: ImageRequest):
    return call(guided_images.generate_character, pid, character_id, body.model, body.budget_usd,
                confirm_paid=body.confirm_paid, request_id=body.request_id, revision=body.revision)


@router.put('/shots/{seq}/characters')
def shot_cast(pid: str, seq: int, body: CastSelection):
    return call(guided_characters.set_shot_characters, pid, seq, body.character_ids, body.revision)


@router.post('/storyboard/reconfirm')
def storyboard_reconfirm(pid: str, body: Revision):
    return call(guided_characters.reconfirm_storyboard, pid, body.revision)


@router.get('/boundaries')
def boundaries(pid: str):
    return call(guided_workflow.get, pid)['boundaries'] or []


@router.get('/boundaries/{boundary_id}/frames')
def boundary_frames(pid: str, boundary_id: str):
    return call(guided_boundaries.list_frames, pid, boundary_id)


@router.post('/boundaries/{boundary_id}/frames/preview')
def boundary_frame_preview(pid: str, boundary_id: str, body: ImagePreview):
    return call(guided_boundaries.preview, pid, boundary_id, body.model)


@router.post('/boundaries/{boundary_id}/frames')
def boundary_frame_generate(pid: str, boundary_id: str, body: ImageRequest):
    return call(guided_boundaries.generate, pid, boundary_id, body.model, body.budget_usd,
                confirm_paid=body.confirm_paid, request_id=body.request_id, revision=body.revision)


@router.post('/boundaries/{boundary_id}/frames/{image_id}/confirm')
def boundary_frame_confirm(pid: str, boundary_id: str, image_id: str, body: BoundaryConfirm):
    return call(guided_boundaries.confirm, pid, boundary_id, image_id, body.revision,
                allow_stale=body.allow_stale)


@router.post('/boundaries/{boundary_id}/frames/{image_id}/crop')
def boundary_frame_crop(pid: str, boundary_id: str, image_id: str, body: Revision):
    return call(guided_boundaries.crop, pid, boundary_id, image_id, body.revision)


@router.delete('/boundaries/{boundary_id}/confirm')
def boundary_unconfirm(pid: str, boundary_id: str, body: Revision):
    return call(guided_boundaries.unconfirm, pid, boundary_id, body.revision)


@router.put('/structure')
def structure(pid: str, body: Structure):
    return call(guided_structure.apply, pid, body.shots, body.revision)


@router.get('/image-attempts')
def image_attempts(pid: str):
    call(guided_characters.get, pid)
    with core.connect() as db:
        return [dict(r) for r in db.execute('SELECT id,target_id,kind,request_id,model,budget,status,image_id,error FROM guided_image_attempts WHERE project_id=? ORDER BY rowid DESC', (pid,))]


@router.post('/image-attempts/{attempt_id}/resolve')
def resolve_image(pid: str, attempt_id: str, body: ResolveImage):
    return call(guided_images.resolve_attempt, pid, attempt_id, confirm_checked=body.confirm_checked)


@router.put('/video-model')
def video_model(pid: str, body: VideoModel):
    return call(guided_generation.set_video_model, pid, body.provider, body.model, body.revision)


@router.post('/shots/{seq}/keep-old-video')
def keep_old_video(pid: str, seq: int, body: Confirm):
    return call(guided_characters.keep_old_video, pid, seq, body.asset_id, body.revision)


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
