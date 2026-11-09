"""Official cloud endpoints. HTTP calls are only made by --live commands."""
import os
import base64
from dataclasses import dataclass
from urllib.parse import urlparse
import httpx

class ProviderError(RuntimeError):
    pass

@dataclass
class Poll:
    status: str
    url: str | None = None
    error: str | None = None

class Provider:
    name = ''
    def __init__(self, client: httpx.Client): self.client = client
    def submit(self, prompt: str, duration: int) -> str: raise NotImplementedError
    def poll(self, remote_id: str) -> Poll: raise NotImplementedError

def request_json(client, method, url, **kwargs):
    r = client.request(method, url, **kwargs)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Avoid logging credentials; preserve vendor's diagnostic response.
        detail = r.text[:1200]
        raise ProviderError(f'HTTP {r.status_code} from {url}: {detail}') from exc
    try: data = r.json()
    except ValueError as e: raise ProviderError('Non-JSON API response') from e
    if not isinstance(data, dict): raise ProviderError('Unexpected API response')
    if 'base_resp' in data and data['base_resp'].get('status_code', 0) != 0:
        raise ProviderError(str(data['base_resp']))
    if data.get('code') and data.get('message'):
        raise ProviderError(str(data['message']))
    return data

def bearer(key):
    if not key: raise ProviderError('Missing API key')
    return {'Authorization': f'Bearer {key}'}

class MiniMax(Provider):
    name = 'minimax'
    base = 'https://api.minimax.io'
    def headers(self): return bearer(os.getenv('MINIMAX_API_KEY'))
    def submit(self, prompt, duration):
        data = request_json(self.client, 'POST', self.base+'/v2/video_generation', headers=self.headers(), json={
            'model':'MiniMax-H3', 'content':[{'type':'text','text':prompt}],
            'resolution':'768P','duration':duration,'ratio':'16:9'})
        rid = data.get('task_id')
        if not rid: raise ProviderError(f'MiniMax missing task_id: {data}')
        return str(rid)
    def submit_i2v(self, prompt, duration, first_frame, mime_type, last_frame=None, last_mime_type=None):
        supported = ('image/png', 'image/jpeg', 'image/webp')
        if mime_type not in supported or not isinstance(first_frame, bytes) or not first_frame:
            raise ValueError('A supported first-frame image is required')
        if last_frame is not None and (last_mime_type not in supported
                or not isinstance(last_frame, bytes) or not last_frame):
            raise ValueError('A supported last-frame image is required')
        content = [{'type':'text','text':prompt},
          {'type':'image_url','image_url':{'url':f'data:{mime_type};base64,' + base64.b64encode(first_frame).decode('ascii')},'role':'first_frame'}]
        if last_frame is not None:
            content.append({'type':'image_url','image_url':{'url':f'data:{last_mime_type};base64,' + base64.b64encode(last_frame).decode('ascii')},'role':'last_frame'})
        data = request_json(self.client, 'POST', self.base+'/v2/video_generation', headers=self.headers(), json={
            'model':'MiniMax-H3', 'content':content,
            'resolution':'768P','duration':duration,'ratio':'adaptive'})
        rid = data.get('task_id')
        if not rid: raise ProviderError('MiniMax missing task_id')
        return str(rid)
    def poll(self, remote_id):
        data = request_json(self.client,'GET',self.base+'/v2/query/video_generation/'+remote_id,headers=self.headers())
        task = data.get('task', data)
        status = str(task.get('status','')).lower()
        if status in ('succeeded','success','completed'):
            url = (task.get('content') or {}).get('url') or task.get('video_url')
            if not url: raise ProviderError('MiniMax success without video URL')
            return Poll('succeeded',url)
        if status in ('failed','cancelled','canceled'): return Poll('failed',error=str(task.get('error',status)))
        if status in ('queued','running','preparing','processing','pending','in_progress'): return Poll('running')
        raise ProviderError(f'Unknown MiniMax status: {status}')

class Flux(Provider):
    name = 'flux'
    model = 'blackforestlabs/flux-3-max/text-to-video'
    i2v_model = 'blackforestlabs/flux-3-max/image-to-video'
    def headers(self):
        key = os.getenv('FAL_KEY')
        if not key: raise ProviderError('Missing FAL_KEY')
        return {'Authorization':'Key '+key}
    def submit(self,prompt,duration):
        data=request_json(self.client,'POST','https://queue.fal.run/'+self.model,headers=self.headers(),json={
            'prompt':prompt,'duration':int(duration),'resolution':'720p',
            'aspect_ratio':'16:9','reasoning_level':'high','generate_audio':True})
        rid=data.get('request_id')
        if not rid: raise ProviderError(f'fal missing request_id: {data}')
        return str(rid)
    def submit_i2v(self, prompt, duration, first_frame, mime_type, last_frame=None, last_mime_type=None):
        supported = ('image/png', 'image/jpeg', 'image/webp')
        if mime_type not in supported or not isinstance(first_frame, bytes) or not first_frame:
            raise ValueError('A supported first-frame image is required')
        if last_frame is not None and (last_mime_type not in supported or not isinstance(last_frame, bytes) or not last_frame):
            raise ValueError('A supported last-frame image is required')
        headers = self.headers()
        import fal_client
        payload = {'prompt': prompt, 'duration': int(duration), 'image_url': fal_client.upload(first_frame, mime_type),
                   'resolution': '720p', 'aspect_ratio': 'auto', 'generate_audio': True}
        if last_frame is not None:
            payload['end_image_url'] = fal_client.upload(last_frame, last_mime_type)
        data = request_json(self.client, 'POST', 'https://queue.fal.run/' + self.i2v_model, headers=headers, json=payload)
        rid = data.get('request_id')
        if not rid: raise ProviderError('fal missing request_id')
        return str(rid)
    def poll(self,remote_id, *, model=None):
        if model is not None and model not in (self.model, self.i2v_model):
            raise ValueError('Unsupported Flux model')
        base='https://queue.fal.run/blackforestlabs/flux-3-max/requests/'+remote_id
        status=request_json(self.client,'GET',base+'/status',headers=self.headers()).get('status','')
        if status == 'COMPLETED':
            data=request_json(self.client,'GET',base,headers=self.headers())
            url=(data.get('video') or {}).get('url')
            if not url: raise ProviderError('fal success without video URL')
            return Poll('succeeded',url)
        if status in ('FAILED','CANCELLED'): return Poll('failed',error=status)
        if status in ('IN_QUEUE','IN_PROGRESS'): return Poll('running')
        raise ProviderError('Unknown fal status: '+str(status))

class Wan(Provider):
    name = 'wan'
    def __init__(self,client):
        super().__init__(client)
        self.base=os.getenv('WAN_BASE_URL','').rstrip('/')
        host=urlparse(self.base).hostname or ''
        if self.base and (not self.base.startswith('https://') or not host.endswith('.maas.aliyuncs.com')):
            raise ProviderError('WAN_BASE_URL must be official https://*.maas.aliyuncs.com workspace endpoint')
    def headers(self): return {**bearer(os.getenv('DASHSCOPE_API_KEY')),'X-DashScope-Async':'enable'}
    def submit(self,prompt,duration):
        if not self.base: raise ProviderError('Missing WAN_BASE_URL')
        data=request_json(self.client,'POST',self.base+'/api/v1/services/aigc/video-generation/video-synthesis',
          headers=self.headers(),json={'model':'wan3.0-video','input':{'prompt':prompt},
          'parameters':{'resolution':'720P','ratio':'16:9','duration':duration,'prompt_extend':False}})
        rid=(data.get('output') or {}).get('task_id')
        if not rid: raise ProviderError(f'Wan missing task_id: {data}')
        return str(rid)
    def poll(self,remote_id):
        if not self.base: raise ProviderError('Missing WAN_BASE_URL')
        data=request_json(self.client,'GET',self.base+'/api/v1/tasks/'+remote_id,headers=bearer(os.getenv('DASHSCOPE_API_KEY')))
        out=data.get('output') or {}
        status=out.get('task_status','')
        if status=='SUCCEEDED':
            if not out.get('video_url'): raise ProviderError('Wan success without video URL')
            return Poll('succeeded',out['video_url'])
        if status in ('FAILED','CANCELED','UNKNOWN'): return Poll('failed',error=str(out.get('message',status)))
        if status in ('PENDING','RUNNING'): return Poll('running')
        raise ProviderError('Unknown Wan status: '+str(status))

PROVIDERS={'minimax':MiniMax,'flux':Flux,'wan':Wan}
