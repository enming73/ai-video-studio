"""Opt-in visual review of immutable offline Director reports.

The estimated charge is a user-provided conservative *per-shot ceiling*, not
provider pricing. Never issues generation requests or changes asset selections.

Live reviews are persistent Jobs: ``review`` enqueues one ``vision_review`` Job
per selected shot, runs them through the worker, then assembles the report.
``execute_shot`` performs the single provider POST and raises the shared
``vision_jobs`` exception classes so the handler can classify outcomes.
"""
from __future__ import annotations
import base64
import json
import os
import re
import tempfile
from pathlib import Path
import httpx
from . import core, director, vision_jobs

SYSTEM = '''You are a cautious film continuity reviewer. Compare the supplied ordered
frames with the storyboard prompt. Do not infer motion from a single still.
Return ONLY a JSON object with keys: character_consistency, scene_match,
action_match, visual_quality (each integer 0..5 or null), findings (list of
short factual observations), uncertainty (string), recommendation (one of
keep, manual_review, regenerate_candidate). Treat weak evidence as manual_review.
Do not claim to have seen frames outside those supplied.'''

NOTE = ('Per-shot ceiling is a user supplied reservation, NOT a verified '
        'provider price. Actual billing may differ.')

_UNKNOWN_ERRORS = (httpx.HTTPError, OSError, ValueError, KeyError,
                   IndexError, TypeError)


def normalize(raw):
    if not isinstance(raw,dict): raise ValueError('Visual review must be a JSON object')
    scores={}
    for key in ('character_consistency','scene_match','action_match','visual_quality'):
        v=raw.get(key)
        if v is not None and (type(v) is not int or not 0<=v<=5):
            raise ValueError(f'Invalid score for {key}')
        scores[key]=v
    rec=raw.get('recommendation')
    if rec not in ('keep','manual_review','regenerate_candidate'):
        raise ValueError('Invalid recommendation')
    findings=raw.get('findings',[])
    if not isinstance(findings,list) or any(not isinstance(x,str) for x in findings):
        raise ValueError('findings must be strings')
    return {**scores,'findings':findings[:12], 'uncertainty':str(raw.get('uncertainty','')),
            'recommendation':rec}


def plan(pid, report_id, per_shot_ceiling_usd, max_budget_usd, selected=None):
    if not 0 < per_shot_ceiling_usd <= 100 or not 0 <= max_budget_usd <= 10000:
        raise ValueError('Invalid budget limits')
    base=director.report(pid,report_id)
    shots=[s for s in base['shots'] if s['frames'] and s['status'] not in ('missing','error')
           and (selected is None or s['seq'] in selected)]
    reserved=round(len(shots)*per_shot_ceiling_usd,6)
    return base,shots,{'shots':len(shots),'reserved_ceiling_usd':reserved,
                       'budget_usd':max_budget_usd,'within_budget':reserved<=max_budget_usd}


def _image_payload(frame, root):
    # Only consume frame files generated in this exact report folder.
    path=Path(frame['file']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Frame is missing or outside report directory')
    if path.stat().st_size>5_000_000: raise ValueError('Frame exceeds 5 MB')
    return {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+
             base64.b64encode(path.read_bytes()).decode('ascii')}}


def _post(endpoint, key, model, content):
    with httpx.Client(timeout=90) as client:
        response = client.post(
            endpoint + '/chat/completions',
            headers={'Authorization': 'Bearer ' + key},
            json={'model': model,
                  'messages': [{'role': 'system', 'content': SYSTEM},
                               {'role': 'user', 'content': content}],
                  'temperature': 0, 'max_tokens': 750})
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


def execute_shot(payload):
    """Perform the single provider POST for one shot and normalize its review."""
    pid = payload['project_id']
    report_id = payload['report_id']
    model = payload['model']
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise vision_jobs.PreRequestFailure('OPENAI_API_KEY missing')
    endpoint = (payload.get('base_url') or os.environ.get('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1').rstrip('/')
    if not endpoint.startswith('https://') and not re.match(
            r'^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)', endpoint):
        raise vision_jobs.PreRequestFailure('Only HTTPS or local HTTP endpoints allowed')
    report_root = core.HOME / pid / 'director-reports' / report_id
    content = [{'type': 'text', 'text':
                f"Shot {payload['seq']}. Storyboard prompt: {payload['prompt']}. "
                'Frames in chronological order:'}]
    try:
        for frame_path in payload['frames']:
            content.append(_image_payload({'file': frame_path}, report_root))
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
    return {'seq': payload['seq'], 'status': 'reviewed', 'review': judgement,
            'usage': data.get('usage', {}),
            'source_asset_id': payload['source_asset_id']}


def _atomic_write(target: Path, text: str) -> None:
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


def assemble(pid, jobs, *, report_id, model, base_url, estimate, review_id,
             run_id, request_id) -> dict:
    """Build the report shape from finished Jobs and persist it atomically."""
    ordered = sorted(jobs, key=lambda j: json.loads(j['payload_json'])['seq'])
    results = []
    for job in ordered:
        payload = json.loads(job['payload_json'])
        if job['status'] == 'SUCCEEDED':
            results.append(json.loads(job['output_json'] or '{}'))
        else:
            detail = (job.get('error_detail') or job.get('error_class')
                      or job.get('block_reason') or job['status'])
            results.append({'seq': payload['seq'], 'status': 'error',
                            'error': str(detail)[:300]})
    report = {
        'schema_version': 1,
        'source_report_id': report_id,
        'project_id': pid,
        'mode': 'vision_live',
        'model': model,
        'budget': estimate,
        'paid_api_calls': len(jobs),
        'results': results,
        'note': NOTE,
        'review_id': review_id,
        'run_id': run_id,
        'job_ids': [j['id'] for j in ordered],
        'request_id': request_id,
        'partial': any(r['status'] == 'error' for r in results),
    }
    output_root = core.HOME / pid / 'director-reviews' / review_id
    output_root.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_root / 'review.json',
                  json.dumps(report, ensure_ascii=False, indent=2))
    return report


def review(pid, report_id, model, base_url=None, live=False, budget_usd=0,
           per_shot_ceiling_usd=0.10, shots=None, request_id=None):
    selected = set(shots) if shots is not None else None
    base, targets, estimate = plan(pid, report_id, per_shot_ceiling_usd,
                                   budget_usd, selected)
    result = {'schema_version': 1, 'source_report_id': report_id,
              'project_id': pid,
              'mode': 'vision_live' if live else 'vision_dry_run',
              'model': model, 'budget': estimate, 'paid_api_calls': 0,
              'results': [], 'note': NOTE}
    if not live:
        return result
    from . import job_runner
    queued = vision_jobs.enqueue_director(
        pid, report_id, model, base_url, budget_usd, per_shot_ceiling_usd,
        selected, request_id)
    states = job_runner.run_until(queued['job_ids'])
    return assemble(pid, states, report_id=report_id, model=model,
                    base_url=base_url, estimate=estimate,
                    review_id=queued['review_id'], run_id=queued['run_id'],
                    request_id=queued['request_id'])
