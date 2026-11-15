"""Official cloud endpoints. HTTP calls are only made by --live commands."""
import os
import base64
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse
import httpx

from .contract import (
    Capability, CostEstimate, ProviderError, RemoteJob, RemoteStatus,
    VideoRequest, to_micro,
)

@dataclass
class Poll:
    status: str
    url: str | None = None
    error: str | None = None

class Provider:
    name = ''
    _NOMINAL = {'768P': (1366, 768), '720p': (1280, 720), '720P': (1280, 720)}
    _FRAME_MIMES = ('image/png', 'image/jpeg', 'image/webp')

    def __init__(self, client): self.client = client
    def submit(self, prompt: str, duration: int) -> str: raise NotImplementedError
    def poll(self, remote_id: str) -> Poll: raise NotImplementedError

    def capabilities(self, model: str = '', *, i2v: bool = False) -> Capability | None:
        return None

    def estimate_cost(self, request: VideoRequest) -> CostEstimate | None:
        return None

    def _validate_request(self, request: VideoRequest, capability: Capability) -> None:
        def invalid(message):
            raise ProviderError(message, kind='invalid_request', submission_state='not_sent')

        if not isinstance(request.prompt, str) or not request.prompt.strip():
            invalid('Prompt is required')
        if (type(request.width) is not int or type(request.height) is not int
                or request.width <= 0 or request.height <= 0):
            invalid('Invalid resolution')
        limits = [self._NOMINAL[item] for item in capability.resolutions
                  if item in self._NOMINAL]
        if not limits or not any(request.width <= width and request.height <= height
                                 for width, height in limits):
            invalid('Unsupported resolution')
        if request.last_frame is not None and request.first_frame is None:
            invalid('A last frame requires a first frame')
        for frame, mime in ((request.first_frame, request.first_frame_mime),
                            (request.last_frame, request.last_frame_mime)):
            if frame is None:
                continue
            if not isinstance(frame, bytes) or not frame:
                invalid('Frame must be nonempty bytes')
            if mime not in self._FRAME_MIMES:
                invalid('Unsupported frame MIME type')

    def submit_video(self, request: VideoRequest, idempotency_key: str) -> RemoteJob:
        capability = self.capabilities(request.model, i2v=request.first_frame is not None)
        if capability is None or request.duration_sec not in capability.durations:
            raise ProviderError('Unsupported duration or model',
                                kind='invalid_request', submission_state='not_sent')
        self._validate_request(request, capability)
        if request.first_frame is not None:
            remote_id = self.submit_i2v(
                request.prompt, request.duration_sec, request.first_frame,
                request.first_frame_mime, request.last_frame, request.last_frame_mime)
        else:
            remote_id = self.submit(request.prompt, request.duration_sec)
        return RemoteJob(self.name, remote_id)

    def status(self, job: RemoteJob, *, model: str | None = None) -> RemoteStatus:
        poll = self.poll(job.remote_id)
        return RemoteStatus({'running': 'running', 'succeeded': 'succeeded',
                             'failed': 'failed'}.get(poll.status, 'unknown'),
                            url=poll.url, error_detail=poll.error)

    def cancel(self, job: RemoteJob) -> bool | None:
        return None


def _rate(env_name: str) -> float | None:
    raw = os.getenv(env_name)
    if raw is None:
        return None
    try:
        rate = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Invalid {env_name}') from exc
    if not 0 < rate < 100:
        raise ValueError(f'Invalid {env_name}')
    return rate


def _estimate(request: VideoRequest, env_name: str) -> CostEstimate | None:
    rate = _rate(env_name)
    if rate is None:
        return None
    raw = os.getenv(env_name)
    return CostEstimate(to_micro(Decimal(str(rate)) * request.duration_sec),
                        f'{env_name}:{raw}', env_name)

class _Classified(ProviderError):
    pass

def _post_state(method):
    return 'maybe_sent' if str(method).upper() == 'POST' else 'not_sent'

def _bounded(value, limit=1200):
    text = str(value)
    return text[:limit] + ('...[truncated]' if len(text) > limit else '')

def _vendor_kind(message):
    text = str(message).lower()
    if any(word in text for word in (
            'api key', 'apikey', 'auth', 'permission', 'unauthorized',
            'balance', 'insufficient')):
        return 'auth'
    if any(word in text for word in (
            'rate limit', 'ratelimit', 'too many', 'frequency', 'throttle')):
        return 'rate_limited'
    if any(word in text for word in (
            'moderation', 'sensitive', 'policy', 'content')):
        return 'content_rejected'
    if any(word in text for word in (
            'internal', 'timeout', 'busy', 'server', 'temporar')):
        return 'transient'
    return 'invalid_request'

def _retry_after(response):
    value = response.headers.get('Retry-After')
    if value is not None and value.isdigit():
        return int(value)
    return None

def request_json(client, method, url, **kwargs):
    try:
        r = client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderError(
            'Provider network failure', kind='unknown_remote',
            submission_state=_post_state(method)) from exc
    status = getattr(r, 'status_code', 200)
    if status >= 400:
        # Avoid logging credentials; preserve a bounded vendor diagnostic.
        detail = r.text[:1200]
        if status in (401, 403):
            kind, state = 'auth', 'not_sent'
        elif status == 429:
            kind, state = 'rate_limited', 'not_sent'
        elif status in (400, 422):
            kind, state = 'invalid_request', 'not_sent'
        elif status in (404, 409):
            if str(method).upper() == 'POST':
                kind, state = 'unknown_remote', 'maybe_sent'
            else:
                kind, state = 'invalid_request', 'not_sent'
        elif status == 408 or 500 <= status < 600:
            kind, state = 'transient', _post_state(method)
        else:
            kind, state = 'invalid_request', 'not_sent'
        raise ProviderError(
            f'HTTP {status} from {url}: {detail}',
            kind=kind, retry_after_sec=_retry_after(r), submission_state=state)
    try: data = r.json()
    except ValueError as e:
        raise ProviderError('Non-JSON API response', kind='unknown_remote',
                            submission_state=_post_state(method)) from e
    if not isinstance(data, dict):
        raise ProviderError('Unexpected API response', kind='unknown_remote',
                            submission_state=_post_state(method))
    if 'base_resp' in data and data['base_resp'].get('status_code', 0) != 0:
        detail = _bounded(data['base_resp'])
        raise ProviderError(detail, kind=_vendor_kind(detail),
                            submission_state='not_sent')
    if data.get('code') and data.get('message'):
        detail = _bounded(data['message'])
        raise ProviderError(detail, kind=_vendor_kind(detail),
                            submission_state='not_sent')
    return data

def bearer(key):
    if not key:
        raise ProviderError('Missing API key', kind='auth', submission_state='not_sent')
    return {'Authorization': f'Bearer {key}'}

class MiniMax(Provider):
    name = 'minimax'
    MODEL = 'MiniMax-H3'
    SOURCE = 'https://platform.minimax.io/docs/api-reference/video-generation-v2-create'
    base = 'https://api.minimax.io'
    def capabilities(self, model: str = '', *, i2v: bool = False) -> Capability | None:
        if model not in ('', self.MODEL):
            return None
        return Capability(tuple(range(4, 16)), ('768P',),
                          supports_first_frame=i2v, supports_last_frame=i2v,
                          supports_cancel=False, supports_idempotency=False,
                          source=self.SOURCE)
    def estimate_cost(self, request: VideoRequest) -> CostEstimate | None:
        return _estimate(request, 'MINIMAX_USD_PER_SECOND')
    def headers(self): return bearer(os.getenv('MINIMAX_API_KEY'))
    def submit(self, prompt, duration):
        data = request_json(self.client, 'POST', self.base+'/v2/video_generation', headers=self.headers(), json={
            'model':'MiniMax-H3', 'content':[{'type':'text','text':prompt}],
            'resolution':'768P','duration':duration,'ratio':'16:9'})
        rid = data.get('task_id')
        if not rid: raise ProviderError(_bounded(f'MiniMax missing task_id: {data}'),
                                        kind='unknown_remote', submission_state='maybe_sent')
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
        if not rid: raise ProviderError('MiniMax missing task_id',
                                        kind='unknown_remote', submission_state='maybe_sent')
        return str(rid)
    def poll(self, remote_id):
        data = request_json(self.client,'GET',self.base+'/v2/query/video_generation/'+remote_id,headers=self.headers())
        task = data.get('task', data)
        status = str(task.get('status','')).lower()
        if status in ('succeeded','success','completed'):
            url = (task.get('content') or {}).get('url') or task.get('video_url')
            if not url: raise ProviderError('MiniMax success without video URL',
                                            kind='unknown_remote', submission_state='confirmed')
            return Poll('succeeded',url)
        if status in ('failed','cancelled','canceled'): return Poll('failed',error=_bounded(task.get('error',status)))
        if status in ('queued','running','preparing','processing','pending','in_progress'): return Poll('running')
        raise ProviderError(f'Unknown MiniMax status: {status}',
                            kind='unknown_remote', submission_state='confirmed')

class Flux(Provider):
    name = 'flux'
    model = 'blackforestlabs/flux-3-max/text-to-video'
    i2v_model = 'blackforestlabs/flux-3-max/image-to-video'
    T2V_SOURCE = ('https://fal.ai/api/openapi/queue/openapi.json?endpoint_id='
                  'blackforestlabs%2Fflux-3-max%2Ftext-to-video')
    I2V_SOURCE = ('https://fal.ai/api/openapi/queue/openapi.json?endpoint_id='
                  'blackforestlabs%2Fflux-3-max%2Fimage-to-video')
    def capabilities(self, model: str = '', *, i2v: bool = False) -> Capability | None:
        expected = self.i2v_model if i2v else self.model
        if model not in ('', expected):
            return None
        return Capability(tuple(range(5, 21)), ('720p',),
                          supports_first_frame=i2v, supports_last_frame=i2v,
                          supports_cancel=False, supports_idempotency=False,
                          source=self.I2V_SOURCE if i2v else self.T2V_SOURCE)
    def estimate_cost(self, request: VideoRequest) -> CostEstimate | None:
        return _estimate(request, 'FAL_USD_PER_SECOND')
    def status(self, job: RemoteJob, *, model: str | None = None) -> RemoteStatus:
        poll = self.poll(job.remote_id, model=model)
        return RemoteStatus({'running': 'running', 'succeeded': 'succeeded',
                             'failed': 'failed'}.get(poll.status, 'unknown'),
                            url=poll.url, error_detail=poll.error)
    def headers(self):
        key = os.getenv('FAL_KEY')
        if not key: raise ProviderError('Missing FAL_KEY', kind='auth', submission_state='not_sent')
        return {'Authorization':'Key '+key}
    def submit(self,prompt,duration):
        data=request_json(self.client,'POST','https://queue.fal.run/'+self.model,headers=self.headers(),json={
            'prompt':prompt,'duration':int(duration),'resolution':'720p',
            'aspect_ratio':'16:9','reasoning_level':'high','generate_audio':True})
        rid=data.get('request_id')
        if not rid: raise ProviderError(_bounded(f'fal missing request_id: {data}'),
                                        kind='unknown_remote', submission_state='maybe_sent')
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
        if not rid: raise ProviderError('fal missing request_id',
                                        kind='unknown_remote', submission_state='maybe_sent')
        return str(rid)
    def poll(self,remote_id, *, model=None):
        if model is not None and model not in (self.model, self.i2v_model):
            raise ValueError('Unsupported Flux model')
        base='https://queue.fal.run/blackforestlabs/flux-3-max/requests/'+remote_id
        status=request_json(self.client,'GET',base+'/status',headers=self.headers()).get('status','')
        if status == 'COMPLETED':
            data=request_json(self.client,'GET',base,headers=self.headers())
            url=(data.get('video') or {}).get('url')
            if not url: raise ProviderError('fal success without video URL',
                                            kind='unknown_remote', submission_state='confirmed')
            return Poll('succeeded',url)
        if status in ('FAILED','CANCELLED'): return Poll('failed',error=status)
        if status in ('IN_QUEUE','IN_PROGRESS'): return Poll('running')
        raise ProviderError('Unknown fal status: '+str(status),
                            kind='unknown_remote', submission_state='confirmed')

class Wan(Provider):
    name = 'wan'
    MODEL = 'wan3.0-video'
    SOURCE = 'https://www.alibabacloud.com/help/en/model-studio/wan3-video-generation-api-reference'
    def capabilities(self, model: str = '', *, i2v: bool = False) -> Capability | None:
        if i2v or model not in ('', self.MODEL):
            return None
        return Capability(tuple(range(2, 31)), ('720P',),
                          supports_first_frame=False, supports_last_frame=False,
                          supports_cancel=False, supports_idempotency=False,
                          source=self.SOURCE)
    def estimate_cost(self, request: VideoRequest) -> CostEstimate | None:
        return _estimate(request, 'WAN_USD_PER_SECOND')
    def __init__(self,client):
        super().__init__(client)
        self.base=os.getenv('WAN_BASE_URL','').rstrip('/')
        host=urlparse(self.base).hostname or ''
        if self.base and (not self.base.startswith('https://') or not host.endswith('.maas.aliyuncs.com')):
            raise ProviderError('WAN_BASE_URL must be official https://*.maas.aliyuncs.com workspace endpoint',
                                kind='invalid_request', submission_state='not_sent')
    def headers(self): return {**bearer(os.getenv('DASHSCOPE_API_KEY')),'X-DashScope-Async':'enable'}
    def submit(self,prompt,duration):
        if not self.base: raise ProviderError('Missing WAN_BASE_URL', kind='auth', submission_state='not_sent')
        data=request_json(self.client,'POST',self.base+'/api/v1/services/aigc/video-generation/video-synthesis',
          headers=self.headers(),json={'model':'wan3.0-video','input':{'prompt':prompt},
          'parameters':{'resolution':'720P','ratio':'16:9','duration':duration,'prompt_extend':False}})
        rid=(data.get('output') or {}).get('task_id')
        if not rid: raise ProviderError(_bounded(f'Wan missing task_id: {data}'),
                                        kind='unknown_remote', submission_state='maybe_sent')
        return str(rid)
    def poll(self,remote_id):
        if not self.base: raise ProviderError('Missing WAN_BASE_URL', kind='auth', submission_state='not_sent')
        data=request_json(self.client,'GET',self.base+'/api/v1/tasks/'+remote_id,headers=bearer(os.getenv('DASHSCOPE_API_KEY')))
        out=data.get('output') or {}
        status=out.get('task_status','')
        if status=='SUCCEEDED':
            if not out.get('video_url'): raise ProviderError('Wan success without video URL',
                                                             kind='unknown_remote', submission_state='confirmed')
            return Poll('succeeded',out['video_url'])
        if status in ('FAILED','CANCELED','UNKNOWN'): return Poll('failed',error=_bounded(out.get('message',status)))
        if status in ('PENDING','RUNNING'): return Poll('running')
        raise ProviderError('Unknown Wan status: '+str(status),
                            kind='unknown_remote', submission_state='confirmed')

PROVIDERS={'minimax':MiniMax,'flux':Flux,'wan':Wan}
