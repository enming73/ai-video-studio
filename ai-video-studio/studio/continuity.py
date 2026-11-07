"""Offline shot-boundary evidence for human continuity review; never uses paid APIs."""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from . import core, director, timeline


def _valid_id(value):
    if not re.fullmatch(r'[0-9a-f]{12}', value):
        raise ValueError('Invalid continuity report ID')
    return value


def analyze(pid):
    shots = sorted(core.storyboard(pid)['shots'], key=lambda s: s['seq'])
    selected = {s['seq']: s.get('selected') for s in timeline.timeline(pid)}
    rid = uuid.uuid4().hex[:12]
    root = core.HOME / pid / 'continuity-reports' / rid
    boundaries = []
    for before, after in zip(shots, shots[1:]):
        left, right = selected.get(before['seq']), selected.get(after['seq'])
        pair = {'from_seq': before['seq'], 'to_seq': after['seq'],
                'from_prompt': before.get('prompt', ''), 'to_prompt': after.get('prompt', ''),
                'from_asset_id': left.get('id') if left else None,
                'to_asset_id': right.get('id') if right else None,
                'status': 'ready', 'warnings': [], 'frames': {},
                'review_questions': [
                    'Does a new character appear without an introduction?',
                    'Do character positions, costumes, or screen direction change abruptly?',
                    'Does the action at the start follow naturally from the previous ending?'],
                'suggested_fixes': [
                    'Keep both shots and adjust the edit if a cut or short hold is sufficient.',
                    'Revise the next shot opening to establish the arriving character or action.',
                    'Insert a short establishing/entrance shot only if necessary.']}
        for side, asset, seq in [('out', left, before['seq']), ('in', right, after['seq'])]:
            if not asset or not Path(asset['file']).is_file():
                pair['warnings'].append(f'Shot {seq}: no selected playable asset')
                pair['status'] = 'missing'
                continue
            try:
                duration = director._probe(Path(asset['file']))['duration']
                # Sample the boundary, not mid-shot: two frames on each side.
                times = ([max(0., duration-0.65), max(0., duration-0.12)] if side == 'out'
                         else [min(duration-0.05, 0.12), min(duration-0.05, 0.65)])
                frames = []
                folder = root / f'boundary-{before["seq"]:03d}-{after["seq"]:03d}'
                folder.mkdir(parents=True, exist_ok=True)
                for i, t in enumerate(times):
                    target = folder / f'{side}-{i+1}.jpg'
                    director._run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
                                   '-ss',f'{max(0.,t):.3f}','-i',asset['file'],
                                   '-frames:v','1','-vf','scale=640:-2','-y',str(target)])
                    frames.append({'time': round(t,3), 'file': str(target)})
                pair['frames'][side] = frames
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                pair['warnings'].append(f'Shot {seq}: {exc}')
                pair['status'] = 'error'
        boundaries.append(pair)
    report = {'schema_version':1, 'report_id':rid, 'project_id':pid,
              'created':core.now(), 'mode':'offline_boundary_evidence',
              'paid_api_calls':0, 'estimated_api_cost_usd':0,
              'disclaimer':'Boundary frames and review questions only; no automated semantic or character continuity verdict.',
              'boundaries':boundaries}
    root.mkdir(parents=True, exist_ok=True)
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def reports(pid):
    with core.connect() as db: core.project(db,pid)
    base = core.HOME / pid / 'continuity-reports'
    output=[]
    for p in sorted(base.glob('*/report.json'), reverse=True):
        try:
            r=json.loads(p.read_text(encoding='utf-8'))
            output.append({'report_id':r['report_id'],'created':r['created'], 'boundaries':len(r['boundaries'])})
        except (OSError, ValueError, KeyError): pass
    return output


def report(pid,rid):
    with core.connect() as db: core.project(db,pid)
    path=core.HOME / pid / 'continuity-reports' / _valid_id(rid) / 'report.json'
    if not path.is_file(): raise ValueError('Continuity report not found')
    return json.loads(path.read_text(encoding='utf-8'))
