"""v0.4.1 opt-in reference image creation and isolated H3 I2V experiments."""
from __future__ import annotations
import base64, hashlib, json, os, time
import httpx
from . import core, references

DEFAULT_IMAGE_MODEL = 'gpt-image-2.5-sunburst'

def _shot(pid, seq):
    return next((s for s in core.storyboard(pid)['shots'] if s['seq'] == seq), None)

def _image_data_uri(path):
    path = references._safe_image(path)
    mime = {'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp'}[path.suffix.lower().lstrip('.')]
    return f'data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}'

def _http_json(client, method, url, **kwargs):
    response = client.request(method, url, **kwargs)
    if response.is_error:
        raise RuntimeError(f'HTTP {response.status_code}: {response.text[:1600]}')
    data = response.json()
    if isinstance(data,dict) and data.get('base_resp',{}).get('status_code',0):
        raise RuntimeError(f'MiniMax: {data["base_resp"]}')
    return data

def _characters_in_shot(pid, seq):
    shot = _shot(pid, seq)
    if not shot: raise ValueError('Unknown shot')
    bible = references.characters(pid)
    manifest = references.reference_manifest(pid)
    # Only attach characters explicitly named in the shot prompt.
    names = [name for name in manifest['characters'] if name.lower() in shot['prompt'].lower()]
    return names

def frame_plan(pid, seq, model=DEFAULT_IMAGE_MODEL):
    shot = _shot(pid, seq)
    if not shot: raise ValueError('Unknown shot')
    manifest = references.reference_manifest(pid)
    names = _characters_in_shot(pid, seq)
    if not names: raise ValueError('No imported character references named in this shot')
    paths = [references._safe_image(manifest['characters'][name]['file']) for name in names]
    prompt = ('Create one cinematic 16:9 landscape storyboard FIRST FRAME for a 3D animated film. '
              'The supplied images are character identity references, not separate panels. '
              'Keep each referenced character’s face, body shape, colors and clothing consistent. '
              'Compose ONE coherent scene, no text, no labels, no split screen. '
              'Capture the beginning of this shot, not its entire motion sequence: ' + shot['prompt'])
    digest = hashlib.sha256(json.dumps({'prompt':prompt,'model':model,'references':[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]},sort_keys=True).encode()).hexdigest()
    return {'project_id':pid,'seq':seq,'model':model,'characters':names,'files':[str(p) for p in paths], 'prompt':prompt,'cache_key':digest,'estimated_cost_usd':None,'existing_first_frame':manifest['shots'].get(str(seq))}

def generate_frame(pid, seq, *, live=False, model=DEFAULT_IMAGE_MODEL, force=False, client=None, budget_usd=None, request_id=None):
    if live: core.require_paid_budget(budget_usd)
    plan = frame_plan(pid,seq,model)
    if not live: return {'dry_run':True, **plan}
    if force and request_id is None:
        raise ValueError('--request-id is required with --force (a new version needs a new id)')
    if request_id is None:
        # Stable id from project, shot and prompt/model/reference digest so a
        # repeated identical live command is idempotent. --force alone does not
        # mint a new id: a new explicit --request-id is required for a new
        # version (the existing asset is retained either way).
        request_id = f'reference:{pid}:{seq}:{plan["cache_key"][:16]}'
    from . import image_jobs, job_runner
    queued = image_jobs.enqueue_reference(pid, seq, model, budget_usd,
                                          confirm_paid=True,
                                          request_id=request_id, force=force)
    job_id = queued['job_id']
    state = job_runner.run_until([job_id])[0]
    if state['status'] != 'SUCCEEDED':
        detail = state.get('error_detail') or state.get('error_class') or ''
        reason = ('blocked (' + str(state.get('block_reason')) + ')'
                  if state['status'] == 'BLOCKED' else state['status'].lower())
        raise RuntimeError(f'Reference frame job {job_id} {reason}: {detail}'.rstrip(': '))
    output = json.loads(state['output_json'] or '{}')
    return {'dry_run':False,'image':output['image'],'generated_file':output['generated_file'],
            'model':model,'characters':output['characters'],'job_id':job_id}

def i2v_plan(pid, seq):
    shot = _shot(pid,seq)
    if not shot: raise ValueError('Unknown shot')
    ref = references.reference_manifest(pid)['shots'].get(str(seq))
    if not ref: raise ValueError('Import or generate an approved first frame first')
    path = references._safe_image(ref['file'])
    if not 4 <= shot['duration'] <= 15: raise ValueError('H3 duration must be 4..15 seconds')
    content = [{'type':'text','text':shot['prompt']},
               {'type':'image_url','image_url':{'url':_image_data_uri(path)},'role':'first_frame'}]
    return {'model':'MiniMax-H3','content':content,'resolution':'768P','duration':shot['duration'],'ratio':'adaptive'}

def i2v(pid, seq, *, live=False, max_usd=None, client=None):
    if live:
        if max_usd is None: raise ValueError('--max-usd required for paid I2V')
        core.require_paid_budget(max_usd)
    body = i2v_plan(pid,seq)
    # Never expose large base64 image or secrets in dry-run output.
    if not live:
        return {'dry_run':True,'model':body['model'],'duration':body['duration'], 'resolution':body['resolution'],
                'first_frame':references.reference_manifest(pid)['shots'][str(seq)]['file'],
                'estimated_video_usd':round(body['duration']*.08,2),'note':'Separate experiment; original shot untouched'}
    if max_usd is None: raise ValueError('--max-usd required for paid I2V')
    estimated = body['duration']*.08
    if max_usd < estimated: raise ValueError(f'Ceiling ${max_usd:.2f} below estimated ${estimated:.2f}')
    if not os.getenv('MINIMAX_API_KEY'): raise ValueError('Missing MINIMAX_API_KEY')
    root = core.HOME / pid / 'experiments' / f'i2v-{seq:03d}'
    root.mkdir(parents=True,exist_ok=True)
    taskfile = root / 'task.json'
    if taskfile.exists():
        raise ValueError(f'Experiment already exists: {taskfile}. Do not resubmit; recover task first.')
    own = client is None
    if own: client = httpx.Client(timeout=120)
    try:
        # Record intention before remote submit; a failed network response may still create a remote task.
        taskfile.write_text(json.dumps({'status':'submitting','seq':seq,'estimate_usd':estimated},indent=2))
        try:
            data = _http_json(client,'POST','https://api.minimax.io/v2/video_generation',
                              headers={'Authorization':'Bearer '+os.environ['MINIMAX_API_KEY']},json=body)
            rid = str(data['task_id'])
            state = {'status':'submitted','seq':seq,'remote_id':rid,'estimate_usd':estimated}
            taskfile.write_text(json.dumps(state,indent=2))
            return state
        except Exception as exc:
            taskfile.write_text(json.dumps({'status':'submission_uncertain','seq':seq,'error':str(exc)[:800]},indent=2))
            raise
    finally:
        if own: client.close()

def poll_i2v(pid,seq,*,client=None):
    root = core.HOME / pid / 'experiments' / f'i2v-{seq:03d}'
    taskfile = root/'task.json'
    if not taskfile.exists(): raise ValueError('No I2V experiment')
    state = json.loads(taskfile.read_text())
    if state['status']=='completed': return state
    rid = state.get('remote_id')
    if not rid: raise ValueError('No remote task ID; inspect provider dashboard before retrying')
    if not os.getenv('MINIMAX_API_KEY'): raise ValueError('Missing MINIMAX_API_KEY')
    own = client is None
    if own: client = httpx.Client(timeout=120,follow_redirects=True)
    try:
        data = _http_json(client,'GET','https://api.minimax.io/v2/query/video_generation/'+rid,
                          headers={'Authorization':'Bearer '+os.environ['MINIMAX_API_KEY']})
        task = data.get('task',data)
        status = str(task.get('status','')).lower()
        if status in ('failed','cancelled','canceled','expired'):
            state['status']='failed';state['error']=str(task.get('error','unknown'))
        elif status in ('succeeded','success','completed'):
            url = (task.get('content') or {}).get('url') or task.get('video_url')
            if not url or not url.startswith('https://'): raise RuntimeError('No valid video URL')
            # Use existing shared media validation, not a bare arbitrary URL download.
            from video_provider_core.media import download
            destination = root/'clip.mp4'
            download(client,url,destination)
            state['status']='completed';state['file']=str(destination)
        else: state['status']='running'
        taskfile.write_text(json.dumps(state,indent=2))
        return state
    finally:
        if own: client.close()
