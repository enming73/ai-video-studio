"""Opt-in, budget-gated visual assessment of adjacent shot boundary frames.

No generation, timeline mutation, or paid calls unless live=True.
"""
from __future__ import annotations
import base64
import json
import os
import re
import tempfile
from pathlib import Path
import httpx
from . import core, continuity, vision_jobs

SYSTEM = """You are a cautious film continuity editor. Compare two ordered frames from
end of shot A with two ordered frames from start of shot B. Do not assume
characters or motion not visible in these frames. Evaluate abrupt entrances,
character appearance/costume, spatial/screen direction, setting, and narrative
transition. Distinguish an intentional cut from a continuity defect. Return
ONLY JSON with: severity (none|low|medium|high|uncertain), confidence (number
0..1), observations (list of short strings), uncertainties (list of strings),
recommendation (keep|manual_review|revise_next_opening|revise_previous_ending|insert_bridge),
reason (string), proposed_prompt (string or null). If evidence is insufficient,
prefer uncertain and manual_review. The proposed prompt must describe a
concrete, minimal fix for the NEXT shot's opening, not claim guaranteed results.
Never imply a paid generation was performed."""
SEVERITIES={'none','low','medium','high','uncertain'}
RECOMMENDATIONS={'keep','manual_review','revise_next_opening','revise_previous_ending','insert_bridge'}

NOTE = ('Ceilings are user-provided estimates, not hard provider spending '
        'caps. Failed requests may incur charges.')

_UNKNOWN_ERRORS = (httpx.HTTPError, OSError, ValueError, KeyError,
                   IndexError, TypeError)


def normalize(data):
    if not isinstance(data,dict): raise ValueError('Expected JSON object')
    if data.get('severity') not in SEVERITIES: raise ValueError('Invalid severity')
    if data.get('recommendation') not in RECOMMENDATIONS: raise ValueError('Invalid recommendation')
    confidence=data.get('confidence')
    if isinstance(confidence,bool) or not isinstance(confidence,(float,int)) or not 0<=confidence<=1:
        raise ValueError('Invalid confidence')
    def strings(key):
        v=data.get(key,[])
        if not isinstance(v,list) or any(not isinstance(x,str) for x in v): raise ValueError(f'Invalid {key}')
        return [x[:600] for x in v[:12]]
    prompt=data.get('proposed_prompt')
    if prompt is not None and not isinstance(prompt,str): raise ValueError('Invalid proposed_prompt')
    return {'severity':data['severity'],'confidence':float(confidence),
            'observations':strings('observations'),'uncertainties':strings('uncertainties'),
            'recommendation':data['recommendation'],'reason':str(data.get('reason',''))[:1200],
            'proposed_prompt':prompt[:5000] if prompt else None}


def _frame(frame,root):
    path=Path(frame['file']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Missing or unsafe frame path')
    if path.stat().st_size>5_000_000: raise ValueError('Frame exceeds 5MB')
    return {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(path.read_bytes()).decode('ascii')}}


def _targets(pid,rid,pairs=None):
    source=continuity.report(pid,rid)
    selected=set(pairs) if pairs is not None else None
    targets=[b for b in source['boundaries'] if b['status']=='ready'
             and len(b['frames'].get('out',[]))==2 and len(b['frames'].get('in',[]))==2
             and (selected is None or b['from_seq'] in selected)]
    return source,targets


def _post(endpoint, key, model, content):
    with httpx.Client(timeout=90) as client:
        response = client.post(
            endpoint + '/chat/completions',
            headers={'Authorization': 'Bearer ' + key},
            json={'model': model,
                  'messages': [{'role': 'system', 'content': SYSTEM},
                               {'role': 'user', 'content': content}],
                  'temperature': 0, 'max_tokens': 1100})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise vision_jobs.AuthFailure(
                    f'Provider rejected the request (HTTP {status})') from exc
            if status == 429:
                raise vision_jobs.RateLimited(
                    'Provider rate limited the request (HTTP 429)') from exc
            if 400 <= status < 500:
                raise vision_jobs.PreRequestFailure(
                    f'Provider rejected the request (HTTP {status})') from exc
            raise
        return response.json()


def _judgement(raw):
    if isinstance(raw, list):
        raw = ''.join(x.get('text', '') for x in raw if isinstance(x, dict))
    if not isinstance(raw, str):
        raise ValueError('Model returned a non-text judgement')
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    return normalize(json.loads(raw))


def execute_pair(payload):
    """Perform the single provider POST for one boundary pair and normalize it."""
    pid = payload['project_id']
    report_id = payload['report_id']
    model = payload['model']
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise vision_jobs.PreRequestFailure('OPENAI_API_KEY missing')
    endpoint = (payload.get('base_url') or os.environ.get('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1').rstrip('/')
    if not endpoint.startswith('https://') and not re.fullmatch(
            r'http://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/.*)?', endpoint):
        raise vision_jobs.PreRequestFailure('Only HTTPS or local HTTP endpoints allowed')
    report_root = core.HOME / pid / 'continuity-reports' / report_id
    content = [{'type': 'text', 'text':
                f"Shot A #{payload['from_seq']} prompt: {payload['from_prompt']}\n"
                f"Shot B #{payload['to_seq']} prompt: {payload['to_prompt']}\n"
                'Images 1-2 are END of A; images 3-4 are START of B. '
                'Explain visible evidence and offer minimal fix if needed.'}]
    try:
        for frame_path in payload['frames']:
            content.append(_frame({'file': frame_path}, report_root))
    except (ValueError, OSError) as exc:
        raise vision_jobs.PreRequestFailure(str(exc)) from exc
    try:
        data = _post(endpoint, key, model, content)
        raw = data['choices'][0]['message']['content']
    except _UNKNOWN_ERRORS as exc:
        raise vision_jobs.UnknownRemote(str(exc)) from exc
    try:
        judgement = _judgement(raw)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise vision_jobs.PostResponseFailure(str(exc)) from exc
    return {'from_seq': payload['from_seq'], 'to_seq': payload['to_seq'],
            'status': 'reviewed', 'review': judgement,
            'usage': data.get('usage', {}),
            'source_asset_ids': payload['source_asset_ids']}


def _atomic_write(target, text):
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix='.review-',
                               suffix='.json')
    os.close(fd)
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def assemble(pid, jobs, *, report_id, model, estimate, review_id, run_id,
             request_id) -> dict:
    """Build the report shape from finished Jobs and persist it atomically."""
    ordered = sorted(jobs, key=lambda j: json.loads(j['payload_json'])['from_seq'])
    results = []
    pairs = []
    for job in ordered:
        payload = json.loads(job['payload_json'])
        pairs.append({'from_seq': payload['from_seq'],
                      'to_seq': payload['to_seq']})
        if job['status'] == 'SUCCEEDED':
            results.append(json.loads(job['output_json'] or '{}'))
        else:
            detail = (job.get('error_detail') or job.get('error_class')
                      or job.get('block_reason') or job['status'])
            results.append({'from_seq': payload['from_seq'],
                            'to_seq': payload['to_seq'], 'status': 'error',
                            'error': str(detail)[:300]})
    report = {
        'schema_version': 1,
        'project_id': pid,
        'source_report_id': report_id,
        'mode': 'vision_live',
        'model': model,
        'budget_usd': estimate['budget_usd'],
        'per_pair_ceiling_usd': estimate['per_pair_ceiling_usd'],
        'reserved_ceiling_usd': estimate['reserved_ceiling_usd'],
        'within_budget': estimate['within_budget'],
        'pairs': pairs,
        'paid_api_calls': len(jobs),
        'results': results,
        'note': NOTE,
        'review_id': review_id,
        'run_id': run_id,
        'job_ids': [j['id'] for j in ordered],
        'request_id': request_id,
        'partial': any(r['status'] == 'error' for r in results),
    }
    output_root = core.HOME / pid / 'continuity-vision-reviews' / review_id
    output_root.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_root / 'review.json',
                  json.dumps(report, ensure_ascii=False, indent=2))
    return report


def review(pid, rid, model, *, live=False, budget_usd=0,
           per_pair_ceiling_usd=0.10, pairs=None, base_url=None, api_key=None,
           timeout=90, request_id=None):
    if not 0 < per_pair_ceiling_usd <= 100 or not 0 <= budget_usd <= 10000:
        raise ValueError('Invalid budget')
    source, targets = _targets(pid, rid, pairs)
    reserve = round(len(targets) * per_pair_ceiling_usd, 6)
    summary = {'schema_version': 1, 'project_id': pid, 'source_report_id': rid,
               'mode': 'vision_live' if live else 'vision_dry_run', 'model': model,
               'budget_usd': budget_usd, 'per_pair_ceiling_usd': per_pair_ceiling_usd,
               'reserved_ceiling_usd': reserve, 'within_budget': reserve <= budget_usd,
               'pairs': [{'from_seq': b['from_seq'], 'to_seq': b['to_seq']}
                         for b in targets],
               'paid_api_calls': 0, 'results': [], 'note': NOTE}
    if not live:
        return summary
    if not summary['within_budget']:
        raise ValueError('Budget insufficient: no API calls made')
    if not model:
        raise ValueError('Model required')
    from . import job_runner
    queued = vision_jobs.enqueue_continuity(
        pid, rid, model, base_url, budget_usd, per_pair_ceiling_usd, pairs,
        request_id)
    states = job_runner.run_until(queued['job_ids'])
    return assemble(pid, states, report_id=rid, model=model,
                    estimate=queued['estimate'], review_id=queued['review_id'],
                    run_id=queued['run_id'], request_id=queued['request_id'])


def reports(pid):
    with core.connect() as db: core.project(db,pid)
    root=core.HOME/pid/'continuity-vision-reviews'
    out=[]
    for path in sorted(root.glob('*/review.json'),reverse=True):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            out.append({'review_id':data['review_id'],'source_report_id':data['source_report_id'],
                        'pairs':len(data['results']),'model':data['model']})
        except (OSError,ValueError,KeyError): pass
    return out


def report(pid,rid):
    with core.connect() as db: core.project(db,pid)
    if not re.fullmatch('[0-9a-f]{12}',rid): raise ValueError('Invalid review ID')
    path=core.HOME/pid/'continuity-vision-reviews'/rid/'review.json'
    if not path.is_file(): raise ValueError('Vision review not found')
    return json.loads(path.read_text(encoding='utf-8'))
