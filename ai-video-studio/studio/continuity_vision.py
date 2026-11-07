"""Opt-in, budget-gated visual assessment of adjacent shot boundary frames.

No generation, timeline mutation, or paid calls unless live=True.
"""
from __future__ import annotations
import base64
import json
import os
import re
import uuid
from pathlib import Path
import httpx
from . import core, continuity

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


def review(pid,rid,model,*,live=False,budget_usd=0,per_pair_ceiling_usd=0.10,
           pairs=None,base_url=None,api_key=None,timeout=90):
    if not 0<per_pair_ceiling_usd<=100 or not 0<=budget_usd<=10000:
        raise ValueError('Invalid budget')
    source,targets=_targets(pid,rid,pairs)
    reserve=round(len(targets)*per_pair_ceiling_usd,6)
    summary={'schema_version':1,'project_id':pid,'source_report_id':rid,
             'mode':'vision_live' if live else 'vision_dry_run','model':model,
             'budget_usd':budget_usd,'per_pair_ceiling_usd':per_pair_ceiling_usd,
             'reserved_ceiling_usd':reserve,'within_budget':reserve<=budget_usd,
             'pairs':[{'from_seq':b['from_seq'],'to_seq':b['to_seq']} for b in targets],
             'paid_api_calls':0,'results':[],
             'note':'Ceilings are user-provided estimates, not hard provider spending caps. Failed requests may incur charges.'}
    if not live: return summary
    if not summary['within_budget']: raise ValueError('Budget insufficient: no API calls made')
    if not model: raise ValueError('Model required')
    key=api_key or os.getenv('OPENAI_API_KEY')
    if not key: raise ValueError('OPENAI_API_KEY missing')
    endpoint=(base_url or os.getenv('OPENAI_BASE_URL') or 'https://api.openai.com/v1').rstrip('/')
    if not endpoint.startswith('https://') and not re.fullmatch(r'http://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/.*)?',endpoint):
        raise ValueError('Only HTTPS or local HTTP endpoints allowed')
    root=core.HOME/pid/'continuity-reports'/rid
    dest=core.HOME/pid/'continuity-vision-reviews'/uuid.uuid4().hex[:12]
    dest.mkdir(parents=True,exist_ok=False)
    summary['review_id']=dest.name
    def persist():
        (dest/'review.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    persist()
    with httpx.Client(timeout=timeout) as client:
        for b in targets:
            try:
                content=[{'type':'text','text':f"Shot A #{b['from_seq']} prompt: {b['from_prompt']}\nShot B #{b['to_seq']} prompt: {b['to_prompt']}\nImages 1-2 are END of A; images 3-4 are START of B. Explain visible evidence and offer minimal fix if needed."}]
                content += [_frame(f,root) for f in b['frames']['out']+b['frames']['in']]
                response=client.post(endpoint+'/chat/completions',headers={'Authorization':'Bearer '+key},
                    json={'model':model,'temperature':0,'max_tokens':1100,'messages':[
                        {'role':'system','content':SYSTEM},{'role':'user','content':content}]})
                response.raise_for_status()
                data=response.json()
                raw=data['choices'][0]['message']['content']
                if isinstance(raw,list): raw=''.join(x.get('text','') for x in raw if isinstance(x,dict))
                raw=raw.strip()
                if raw.startswith('```'): raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
                judgement=normalize(json.loads(raw))
                summary['results'].append({'from_seq':b['from_seq'],'to_seq':b['to_seq'],
                    'status':'reviewed','source_asset_ids':[b['from_asset_id'],b['to_asset_id']],
                    'review':judgement,'usage':data.get('usage',{})})
            except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError) as exc:
                summary['results'].append({'from_seq':b['from_seq'],'to_seq':b['to_seq'],
                                          'status':'error','error':str(exc)[:300]})
            finally:
                summary['paid_api_calls']+=1
                persist()
    return summary


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
