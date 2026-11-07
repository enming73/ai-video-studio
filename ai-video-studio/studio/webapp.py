"""Local-only REST API for v0.7 React timeline editor. No paid AI calls."""
from __future__ import annotations
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from . import core, timeline, sound

app = FastAPI(title="AI Video Studio", version="0.8.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

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
def health(): return {'status':'ok','version':'0.8.0'}
