"""Non-destructive, offline evidence gathering for the Director Agent.

Scores here are technical heuristics, not semantic/visual LLM judgements.
No network access or generation API calls are performed.
"""
from __future__ import annotations
import json
import math
import re
import subprocess
import uuid
from pathlib import Path
from . import core, timeline


def _run(args, timeout=90):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Analysis tool unavailable or timed out: {args[0]}') from exc
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-600:])
    return p.stdout, p.stderr


def _probe(path):
    out, _ = _run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate','-of','json',str(path)])
    info = json.loads(out)
    videos = [s for s in info.get('streams',[]) if s.get('codec_type')=='video']
    if not videos:
        raise ValueError('No video stream')
    duration = float(info.get('format',{}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid duration')
    v = videos[0]
    return {'duration':round(duration,3),'width':v.get('width'), 'height':v.get('height'),
            'codec':v.get('codec_name'),'fps':v.get('avg_frame_rate')}


def _detect(path, filter_spec, marker):
    # Metadata only; never modifies source files.
    _, err = _run(['ffmpeg','-hide_banner','-nostdin','-i',str(path),'-vf',filter_spec,
                   '-an','-f','null','-'],timeout=180)
    matches=[]
    for line in err.splitlines():
        if marker not in line: continue
        values=dict((k,float(v)) for k,v in re.findall(r'([a-z_]+):\s*(-?\d+(?:\.\d+)?)',line))
        if values: matches.append(values)
    return matches


def _frames(path, folder, duration, count):
    folder.mkdir(parents=True,exist_ok=True)
    result=[]
    for idx in range(count):
        t=min(max(0.0,duration-0.05),duration*(idx+1)/(count+1))
        target=folder/f'frame-{idx+1:02d}.jpg'
        _run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-ss',f'{t:.3f}',
              '-i',str(path),'-frames:v','1','-vf','scale=640:-2', '-y',str(target)])
        result.append({'time':round(t,3),'file':str(target)})
    return result


def analyze(pid, frames=3, black=True, freeze=True):
    if not 1 <= frames <= 8: raise ValueError('frames must be 1..8')
    shots=core.storyboard(pid)['shots']
    selections={x['seq']:x['selected'] for x in timeline.timeline(pid)}
    report_id=uuid.uuid4().hex[:12]
    root=core.HOME/pid/'director-reports'/report_id
    results=[]
    for shot in shots:
        seq=shot['seq']; asset=selections.get(seq)
        entry={'seq':seq,'shot_id':shot['id'],'prompt':shot['prompt'],
               'planned_duration':shot['duration'],'asset_id':asset['id'] if asset else None,
               'checks':{},'warnings':[],'frames':[],'status':'unknown'}
        if not asset:
            entry['warnings'].append('No selected video asset')
            entry['status']='missing'
            results.append(entry); continue
        path=Path(asset['file'])
        if not path.is_file():
            entry['warnings'].append('Selected video file missing')
            entry['status']='missing'
            results.append(entry); continue
        try:
            probe=_probe(path)
            entry['checks']['probe']=probe
            drift=abs(probe['duration']-shot['duration'])
            entry['checks']['duration_drift_seconds']=round(drift,3)
            if drift>0.5: entry['warnings'].append(f'Duration differs from plan by {drift:.2f}s')
            if probe['width']<640 or probe['height']<360: entry['warnings'].append('Low resolution')
            if black:
                events=_detect(path,'blackdetect=d=0.25:pix_th=0.10','black_start:')
                entry['checks']['black_segments']=events
                if events: entry['warnings'].append(f'{len(events)} black segment(s) detected')
            if freeze:
                events=_detect(path,'freezedetect=n=-50dB:d=1.0','freeze_start:')
                entry['checks']['freeze_segments']=events
                if events: entry['warnings'].append(f'{len(events)} freeze segment(s) detected')
            entry['frames']=_frames(path,root/f'shot-{seq:03d}',probe['duration'],frames)
            entry['status']='review' if entry['warnings'] else 'technical_pass'
        except (ValueError,RuntimeError,OSError) as exc:
            entry['status']='error'; entry['warnings'].append(str(exc))
        results.append(entry)
    report={'schema_version':1,'report_id':report_id,'project_id':pid,'created':core.now(),
            'mode':'offline_technical_only','paid_api_calls':0,'estimated_api_cost_usd':0,
            'disclaimer':'Technical checks only. Character continuity, motion-story alignment and aesthetics require visual review.',
            'summary':{'total':len(results),'technical_pass':sum(r['status']=='technical_pass' for r in results),
                       'needs_review':sum(r['status']!='technical_pass' for r in results)},
            'shots':results}
    root.mkdir(parents=True,exist_ok=True)
    target=root/'report.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def reports(pid):
    with core.connect() as db: core.project(db,pid)
    folder=core.HOME/pid/'director-reports'
    if not folder.exists(): return []
    output=[]
    for file in sorted(folder.glob('*/report.json'),reverse=True):
        try:
            data=json.loads(file.read_text(encoding='utf-8'))
            output.append({k:data[k] for k in ('report_id','created','mode','summary')})
        except (ValueError,KeyError,OSError): continue
    return output


def report(pid,rid):
    with core.connect() as db: core.project(db,pid)
    if not re.fullmatch(r'[a-f0-9]{12}',rid): raise ValueError('Invalid report ID')
    path=core.HOME/pid/'director-reports'/rid/'report.json'
    if not path.is_file(): raise ValueError('Report not found')
    return json.loads(path.read_text(encoding='utf-8'))
