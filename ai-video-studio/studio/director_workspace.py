"""Read-only Director review browser and independent human decision log."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from . import core, director

VALID = {'accept_keep', 'investigate', 'reject_suggestion', 'approve_regeneration'}

def _review_dir(pid):
    with core.connect() as db: core.project(db,pid)
    return core.HOME/pid/'director-reviews'

def _read(pid, rid):
    if not rid or any(c not in '0123456789abcdef' for c in rid) or len(rid)!=12:
        raise ValueError('Invalid review ID')
    path=_review_dir(pid)/rid/'review.json'
    if not path.is_file(): raise ValueError('Review not found')
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('project_id')!=pid: raise ValueError('Review project mismatch')
    return data

def reviews(pid):
    base=_review_dir(pid)
    if not base.exists(): return []
    results=[]
    for f in sorted(base.glob('*/review.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        try:
            r=_read(pid,f.parent.name)
            results.append({'review_id':f.parent.name,'source_report_id':r.get('source_report_id'),
                            'model':r.get('model'),'results':len(r.get('results',[])),
                            'created_at':datetime.fromtimestamp(f.stat().st_mtime,timezone.utc).isoformat()})
        except (ValueError,OSError,KeyError,json.JSONDecodeError): continue
    return results

def review(pid,rid):
    data=_read(pid,rid)
    report=director.report(pid,data['source_report_id'])
    frames={s['seq']:{'count':len(s.get('frames',[])),'prompt':s.get('prompt'),
                       'asset_id':s.get('asset_id'),'warnings':s.get('warnings',[])} for s in report['shots']}
    data['shot_evidence']=frames
    data['decisions']=decisions(pid,rid)
    return data

def decisions(pid,rid):
    _read(pid,rid)
    path=_review_dir(pid)/rid/'decisions.json'
    if not path.exists(): return {}
    data=json.loads(path.read_text(encoding='utf-8'))
    return data if isinstance(data,dict) else {}

def decide(pid,rid,seq,action,note=''):
    data=_read(pid,rid)
    if action not in VALID: raise ValueError('Invalid decision')
    if not isinstance(seq,int) or seq<1 or not any(s.get('seq')==seq for s in data.get('results',[])):
        raise ValueError('Shot was not reviewed')
    if not isinstance(note,str) or len(note)>2000: raise ValueError('Note too long')
    path=_review_dir(pid)/rid/'decisions.json'
    values=decisions(pid,rid)
    values[str(seq)]={'action':action,'note':note,'timestamp':datetime.now(timezone.utc).isoformat(),
                      'source_asset_id':next(s.get('source_asset_id') for s in data['results'] if s.get('seq')==seq)}
    temp=path.with_name(f'.decisions-{uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(values,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(path)
    finally: temp.unlink(missing_ok=True)
    # Explicitly records approval only. Never calls paid APIs or queues generation.
    return values[str(seq)]
