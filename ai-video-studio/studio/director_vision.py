"""Opt-in visual review of immutable offline Director reports.

The estimated charge is a user-provided conservative *per-shot ceiling*, not
provider pricing. Never issues generation requests or changes asset selections.
"""
from __future__ import annotations
import base64
import json
import os
import re
import uuid
from pathlib import Path
import httpx
from . import core, director

SYSTEM = '''You are a cautious film continuity reviewer. Compare the supplied ordered
frames with the storyboard prompt. Do not infer motion from a single still.
Return ONLY a JSON object with keys: character_consistency, scene_match,
action_match, visual_quality (each integer 0..5 or null), findings (list of
short factual observations), uncertainty (string), recommendation (one of
keep, manual_review, regenerate_candidate). Treat weak evidence as manual_review.
Do not claim to have seen frames outside those supplied.'''


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


def review(pid,report_id,model,base_url=None,live=False,budget_usd=0,
           per_shot_ceiling_usd=0.10,shots=None,api_key=None,timeout=90):
    selected=set(shots) if shots is not None else None
    base,targets,estimate=plan(pid,report_id,per_shot_ceiling_usd,budget_usd,selected)
    result={'schema_version':1,'source_report_id':report_id,'project_id':pid,
            'mode':'vision_live' if live else 'vision_dry_run','model':model,
            'budget':estimate,'paid_api_calls':0,'results':[],
            'note':'Per-shot ceiling is a user supplied reservation, NOT a verified provider price. Actual billing may differ.'}
    if not live: return result
    if not estimate['within_budget']: raise ValueError('Budget insufficient: no requests made')
    if not model: raise ValueError('Model is required')
    key=api_key or os.environ.get('OPENAI_API_KEY')
    if not key: raise ValueError('OPENAI_API_KEY missing')
    endpoint=(base_url or os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').rstrip('/')
    if not endpoint.startswith('https://') and not re.match(r'^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)',endpoint):
        raise ValueError('Only HTTPS or local HTTP endpoints allowed')
    report_root=core.HOME/pid/'director-reports'/report_id
    output_root=core.HOME/pid/'director-reviews'/uuid.uuid4().hex[:12]
    output_root.mkdir(parents=True,exist_ok=False)
    result['review_id']=output_root.name
    with httpx.Client(timeout=timeout) as client:
        for shot in targets:
            try:
                content=[{'type':'text','text':f"Shot {shot['seq']}. Storyboard prompt: {shot['prompt']}. Frames in chronological order:"}]
                content += [_image_payload(f,report_root) for f in shot['frames']]
                response=client.post(endpoint+'/chat/completions',headers={'Authorization':'Bearer '+key},
                    json={'model':model,'messages':[{'role':'system','content':SYSTEM},
                        {'role':'user','content':content}],'temperature':0,'max_tokens':750})
                response.raise_for_status()
                data=response.json()
                raw=data['choices'][0]['message']['content']
                if isinstance(raw,list): raw=''.join(x.get('text','') for x in raw if isinstance(x,dict))
                raw=raw.strip()
                if raw.startswith('```'):
                    raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
                judgement=normalize(json.loads(raw))
                result['results'].append({'seq':shot['seq'],'status':'reviewed','review':judgement,
                    'usage':data.get('usage',{}),'source_asset_id':shot['asset_id']})
            except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError) as exc:
                result['results'].append({'seq':shot['seq'],'status':'error','error':str(exc)[:300]})
            finally:
                # Record attempts even when response fails: provider may still charge.
                result['paid_api_calls']+=1
                (output_root/'review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
