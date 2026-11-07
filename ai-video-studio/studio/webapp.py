"""Local-only REST API for v0.7 React timeline editor. No paid AI calls."""
from __future__ import annotations
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import core, timeline, sound

app = FastAPI(title="AI Video Studio", version="0.9.3", docs_url="/api/docs", openapi_url="/api/openapi.json")

def checked_pid(pid: str):
    with core.connect() as db: core.project(db,pid)
    return pid

def api_call(fn,*args,**kwargs):
    try: return fn(*args,**kwargs)
    except (ValueError,RuntimeError) as exc: raise HTTPException(400,str(exc)) from exc

class Selection(BaseModel):
    asset_id: str

class AudioEvent(BaseModel):
    file: str
    at: float = Field(ge=0)
    duration: float | None = Field(default=None,gt=0)
    gain: float = Field(default=1.0,ge=0,le=3)
    fade_in: float = Field(default=0,ge=0)
    fade_out: float = Field(default=0,ge=0)

@app.get('/api/projects')
def projects():
    with core.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM projects ORDER BY created DESC')]

@app.get('/api/projects/{pid}')
def project(pid: str):
    return api_call(core.storyboard,pid)

@app.post('/api/projects/{pid}/sync')
def sync(pid: str):
    return api_call(timeline.sync,pid)

@app.get('/api/projects/{pid}/timeline')
def get_timeline(pid: str):
    return api_call(timeline.timeline,pid)

@app.get('/api/projects/{pid}/assets')
def get_assets(pid: str):
    return api_call(timeline.assets,pid)

@app.put('/api/projects/{pid}/shots/{seq}/selection')
def select(pid: str,seq: int, body: Selection):
    return {'asset_id':api_call(timeline.select,pid,seq,body.asset_id)}

@app.get('/api/projects/{pid}/audio')
def audio(pid: str):
    return api_call(sound.audio_list,pid)

@app.post('/api/projects/{pid}/audio')
def add_audio(pid: str, body: AudioEvent):
    return api_call(sound.audio_add,pid,body.file,body.at,body.duration,body.gain,body.fade_in,body.fade_out)

class AudioEdit(BaseModel):
    at: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, gt=0)
    gain: float | None = Field(default=None, ge=0, le=3)

@app.patch('/api/projects/{pid}/audio/{event_id}')
def edit_audio(pid: str, event_id: str, body: AudioEdit):
    return api_call(sound.audio_update, pid, event_id, **body.model_dump(exclude_none=True))

@app.delete('/api/projects/{pid}/audio/{event_id}')
def delete_audio(pid: str,event_id: str):
    return api_call(sound.audio_remove,pid,event_id)

@app.get('/api/projects/{pid}/soundtrack')
def soundtrack(pid: str):
    return api_call(sound.config,pid)

@app.get('/api/projects/{pid}/media/{kind}/{seq}')
def media(pid: str,kind: str,seq: int):
    checked_pid(pid)
    if kind not in ('selected','original','i2v'): raise HTTPException(400,'Invalid media kind')
    assets=api_call(timeline.assets,pid,seq)
    item=next((a for a in assets if (a['selected'] if kind=='selected' else a['source']==kind)),None)
    if not item: raise HTTPException(404,'No media for this shot')
    path=Path(item['file']).resolve()
    # Assets are explicitly registered in SQLite; never accept arbitrary filesystem paths from URL.
    if not path.is_file(): raise HTTPException(404,'Missing asset file')
    return FileResponse(path,media_type='video/mp4',filename=path.name,content_disposition_type='inline',headers={'Cache-Control':'no-store, max-age=0'})

@app.get('/api/projects/{pid}/audio-media/{event_id}')
def audio_media(pid: str,event_id: str):
    event=next((x for x in api_call(sound.audio_list,pid) if x['id']==event_id),None)
    if not event: raise HTTPException(404,'Unknown audio event')
    path=Path(event['file']).resolve()
    if not path.is_file(): raise HTTPException(404,'Missing audio file')
    return FileResponse(path,content_disposition_type='inline')

@app.get('/api/health')
def health(): return {'status':'ok','version':'0.9.3'}


# One local FFmpeg job at a time. No shell execution, no user-supplied output path.
_render_pool = ThreadPoolExecutor(max_workers=1)
_render_lock = Lock()
_render_jobs: dict[str, dict] = {}

class RenderRequest(BaseModel):
    source: str = 'selected'
    burn_subtitles: bool = False
    ducking: bool = True

@app.post('/api/projects/{pid}/renders', status_code=202)
def start_render(pid: str, body: RenderRequest):
    checked_pid(pid)
    if body.source not in ('selected','original','i2v','mixed'):
        raise HTTPException(400,'Invalid source')
    with _render_lock:
        if any(j['status'] in ('queued','running') for j in _render_jobs.values()):
            raise HTTPException(409,'A render is already queued or running')
        jid=uuid4().hex
        job={'id':jid,'project_id':pid,'status':'queued','output':None,'error':None,'created':datetime.now(timezone.utc).isoformat()}
        _render_jobs[jid]=job
    def work():
        with _render_lock: job['status']='running'
        try:
            out=sound.export(pid,source=body.source,burn_subtitles=body.burn_subtitles,ducking=body.ducking)
            with _render_lock:
                job['output']=str(out)
                job['status']='completed'
        except Exception as exc:
            with _render_lock:
                job['error']=str(exc)
                job['status']='failed'
    _render_pool.submit(work)
    return dict(job)

@app.get('/api/projects/{pid}/renders/{jid}')
def render_status(pid: str,jid: str):
    checked_pid(pid)
    with _render_lock:
        job=_render_jobs.get(jid)
        if not job or job['project_id']!=pid: raise HTTPException(404,'Unknown render job')
        return dict(job)

@app.get('/api/projects/{pid}/renders/{jid}/video')
def render_video(pid: str,jid: str):
    job=render_status(pid,jid)
    if job['status']!='completed': raise HTTPException(409,'Render not completed')
    path=Path(job['output']).resolve()
    if not path.is_file() or not path.is_relative_to((core.HOME/pid).resolve()):
        raise HTTPException(404,'Render missing')
    return FileResponse(path,media_type='video/mp4',filename=path.name,headers={'Cache-Control':'no-store'})

@app.post('/api/projects/{pid}/director/analyze')
def director_analyze(pid: str, frames: int = 3):
    from . import director
    return api_call(director.analyze,pid,frames)

@app.get('/api/projects/{pid}/director/reports')
def director_reports(pid: str):
    from . import director
    return api_call(director.reports,pid)

@app.get('/api/projects/{pid}/director/reports/{rid}')
def director_report(pid: str,rid: str):
    from . import director
    return api_call(director.report,pid,rid)

@app.get('/api/projects/{pid}/director/reports/{rid}/shots/{seq}/frames/{index}')
def director_frame(pid: str,rid: str,seq: int,index: int):
    from . import director
    data=api_call(director.report,pid,rid)
    shot=next((s for s in data['shots'] if s['seq']==seq),None)
    if not shot or index<0 or index>=len(shot['frames']): raise HTTPException(404,'Frame not found')
    path=Path(shot['frames'][index]['file']).resolve()
    allowed=(core.HOME/pid/'director-reports'/rid).resolve()
    if not path.is_relative_to(allowed) or not path.is_file(): raise HTTPException(404,'Frame missing')
    return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


# Director Workspace: read existing reviews and store human decisions only.
class DirectorDecision(BaseModel):
    action: str
    note: str = Field(default='', max_length=2000)

@app.get('/api/projects/{pid}/director/reviews')
def director_review_list(pid: str):
    from . import director_workspace
    return api_call(director_workspace.reviews,pid)

@app.get('/api/projects/{pid}/director/reviews/{rid}')
def director_review_detail(pid: str,rid: str):
    from . import director_workspace
    return api_call(director_workspace.review,pid,rid)

@app.put('/api/projects/{pid}/director/reviews/{rid}/shots/{seq}/decision')
def director_review_decision(pid: str,rid: str,seq: int,body: DirectorDecision):
    from . import director_workspace
    return api_call(director_workspace.decide,pid,rid,seq,body.action,body.note)


# Production workflow: the browser calls the same core operations as the CLI.
# No subprocess shell, no paid calls without a separate explicit live request.
_production_pool = ThreadPoolExecutor(max_workers=1)
_production_lock = Lock()
_production_jobs: dict[str, dict] = {}

class CreateProjectRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=5000)
    duration: int = Field(default=60, ge=1, le=600)
    provider: str = Field(default='mock')
    budget: float = Field(default=0.0, ge=0, le=10000)

class PlanProjectRequest(BaseModel):
    shot_length: int = Field(default=10, ge=1, le=30)

class GenerateProjectRequest(BaseModel):
    live: bool = False
    budget: float | None = Field(default=None, ge=0, le=10000)
    limit: int = Field(default=0, ge=0, le=1000)
    confirm_paid: bool = False
    confirmation_project_id: str | None = None

@app.post('/api/production/projects', status_code=201)
def production_create(body: CreateProjectRequest):
    pid = api_call(core.create,body.prompt,body.duration,body.provider,body.budget)
    return {'project_id':pid,'project':api_call(core.storyboard,pid)['project']}

@app.post('/api/projects/{pid}/production/plan')
def production_plan(pid: str,body: PlanProjectRequest):
    checked_pid(pid)
    count=api_call(core.plan,pid,body.shot_length)
    return {'planned_shots':count,'storyboard':api_call(core.storyboard,pid)}

@app.post('/api/projects/{pid}/production/estimate')
def production_estimate(pid: str,body: GenerateProjectRequest):
    checked_pid(pid)
    # Dry-run is always forced here, regardless of the submitted live flag.
    return {'estimate':api_call(core.generate,pid,live=False,budget=body.budget,limit=body.limit)}

def _production_execute(jid: str,pid: str,budget: float,limit: int):
    with _production_lock:
        job=_production_jobs[jid]; job['status']='running'
        try:
            result=core.generate(pid,live=True,budget=budget,limit=limit)
            job['result']=result; job['status']='completed'
        except Exception as exc:
            job['status']='failed'; job['error']=str(exc)[:1000]

@app.post('/api/projects/{pid}/production/generate',status_code=202)
def production_generate(pid: str,body: GenerateProjectRequest):
    checked_pid(pid)
    if not body.live:
        return {'status':'dry_run','estimate':api_call(core.generate,pid,live=False,budget=body.budget,limit=body.limit)}
    if not body.confirm_paid or body.confirmation_project_id!=pid:
        raise HTTPException(400,'Paid generation requires explicit confirmation and matching project ID')
    if body.budget is None or body.budget<=0:
        raise HTTPException(400,'Paid generation requires an explicit positive per-run budget')
    with core.connect() as db:
        p=core.project(db,pid)
        if p['provider']=='mock':
            raise HTTPException(400,'Mock projects are not paid cloud projects')
        if body.budget>p['budget']:
            raise HTTPException(400,'Run budget cannot exceed project budget')
    # Preflight before queuing; still no provider call.
    estimate=api_call(core.generate,pid,live=False,budget=body.budget,limit=body.limit)
    jid=uuid4().hex
    job={'id':jid,'project_id':pid,'status':'queued','estimate':estimate,'result':None,'error':None}
    _production_jobs[jid]=job
    _production_pool.submit(_production_execute,jid,pid,body.budget,body.limit)
    return job

@app.get('/api/projects/{pid}/production/jobs/{jid}')
def production_job(pid: str,jid: str):
    checked_pid(pid)
    job=_production_jobs.get(jid)
    if not job or job['project_id']!=pid: raise HTTPException(404,'Job not found')
    return job
