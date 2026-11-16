"""v0.6.1: storyboard narration planning, TTS, synchronized shot-level SRT.

Paid calls are opt-in; original soundtrack/video and generated assets are preserved.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
import httpx
from dotenv import load_dotenv
from . import core, sound


def _root(pid):
    with core.connect() as db:
        core.project(db, pid)
    root = core.HOME / pid / 'narration'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _shots(pid):
    with core.connect() as db:
        core.project(db, pid)
        return [dict(x) for x in db.execute('SELECT seq,prompt,duration FROM shots WHERE project_id=? ORDER BY seq',(pid,))]


def _script_path(pid): return _root(pid) / 'script.json'


class UnknownRemote(Exception):
    """The LLM request may have been sent but its outcome is unknown."""


class ConfigError(ValueError):
    """Deterministic local configuration problem; no TTS request was sent."""


_UNKNOWN_ERRORS = (httpx.HTTPError, OSError, json.JSONDecodeError,
                   KeyError, IndexError, TypeError)

_NARRATION_SYSTEM = (
    'Write a warm, concise narration for a short film. Return ONLY JSON with key "lines": '
    'an array of objects {"seq": integer, "text": string}. Each line must be spoken '
    'comfortably within its shot duration. Do not narrate camera directions. '
    'Keep characters and story consistent. Do not add facts absent from the visual story.')


def generate_script(payload):
    """Pure LLM prompt construction and validation; no project or file writes."""
    shots = payload['shots']
    prompt = {'story': payload['prompt'], 'language': payload['language'],
              'shots': [{'seq': s['seq'], 'duration': s['duration'],
                         'visual_prompt': s['prompt']} for s in shots]}
    try:
        data = core._llm_json(_NARRATION_SYSTEM, json.dumps(prompt, ensure_ascii=False),
                              payload['model'], payload['base_url'])
    except _UNKNOWN_ERRORS as exc:
        raise UnknownRemote(str(exc)) from exc
    if not isinstance(data, dict):
        raise UnknownRemote('LLM returned a non-object response')
    lines = data.get('lines')
    if not isinstance(lines, list) or len(lines) != len(shots):
        raise ValueError('LLM returned wrong line count')
    by_seq = {}
    for item in lines:
        if not isinstance(item, dict) or not isinstance(item.get('seq'), int) or not isinstance(item.get('text'), str):
            raise ValueError('Invalid LLM narration line')
        if item['seq'] in by_seq or not item['text'].strip() or len(item['text']) > 600:
            raise ValueError('Duplicate, empty, or too-long narration line')
        by_seq[item['seq']] = item['text'].strip()
    if set(by_seq) != {s['seq'] for s in shots}:
        raise ValueError('Narration sequence mismatch')
    return {'project_id': payload['project_id'], 'language': payload['language'],
            'model': payload['model'],
            'lines': [{'seq': s['seq'], 'duration': s['duration'],
                       'text': by_seq[s['seq']]} for s in shots]}


def plan(pid, language='zh-CN', model=None, live=False, base_url=None, *,
         budget_usd=None, request_id=None):
    if live: core.require_paid_budget(budget_usd)
    shots = _shots(pid)
    if not shots: raise ValueError('Project has no storyboard')
    target = _script_path(pid)
    if target.exists(): raise ValueError(f'Script already exists: {target}; edit it directly or back it up first')
    if not live:
        return {'dry_run': True, 'shots': len(shots), 'language': language, 'model': model, 'paid_request': bool(model)}
    if not model: raise ValueError('Provide --model to use the LLM')
    if request_id is None:
        request_id = f'narration-{pid}'
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    from . import narration_jobs, job_runner
    queued = narration_jobs.enqueue_plan(pid, language=language, model=model,
                                         base_url=base_url, budget_usd=budget_usd,
                                         request_id=request_id)
    job_id = queued['job_id']
    state = job_runner.run_until([job_id])[0]
    if state['status'] != 'SUCCEEDED':
        detail = state.get('error_detail') or state.get('error_class') or ''
        if state['status'] == 'BLOCKED':
            reason = f'blocked ({state.get("block_reason")})'
        else:
            reason = state['status'].lower()
        raise RuntimeError(f'Narration plan {reason}: {detail}'.rstrip(': '))
    output = json.loads(state['output_json'] or '{}')
    return {'script': output.get('script'), 'lines': output.get('lines'),
            'job_id': job_id}


def show(pid):
    target=_script_path(pid)
    if not target.is_file(): raise ValueError('No narration script; run narration-plan')
    data=json.loads(target.read_text(encoding='utf-8'))
    shots=_shots(pid)
    lines=data.get('lines')
    if not isinstance(lines,list) or len(lines)!=len(shots): raise ValueError('Script line count mismatch')
    for s,line in zip(shots,lines):
        if (line.get('seq')!=s['seq'] or line.get('duration')!=s['duration']
                or not isinstance(line.get('text'),str) or not line['text'].strip()):
            raise ValueError(f'Invalid narration for shot {s["seq"]}')
        if len(line['text'])>600: raise ValueError('Narration line too long')
    return data


def _srt(lines, target):
    start=0.0; blocks=[]
    for i,line in enumerate(lines,1):
        end=start+float(line['duration'])
        # Shot-aligned subtitles: not word-level forced alignment.
        blocks.extend([str(i),f'{sound._srt_timestamp(start)} --> {sound._srt_timestamp(end)}',
                       re.sub(r'[\r\n]+',' ',line['text']).strip(),''])
        start=end
    target.write_text('\n'.join(blocks),encoding='utf-8')


def _speed_filter(actual, desired):
    ratio=actual/desired
    if ratio>1.5: raise ValueError(f'TTS audio {actual:.1f}s exceeds shot {desired:.1f}s by >50%; shorten the narration text')
    if ratio<0.5: return 'anull'
    return f'atempo={ratio:.5f}'


def require_tts_config(base_url=None):
    """Fail fast, before any request, on a missing key or insecure endpoint."""
    load_dotenv()
    if not (os.getenv('STUDIO_TTS_API_KEY') or os.getenv('STUDIO_LLM_API_KEY')):
        raise ConfigError('Set STUDIO_TTS_API_KEY or STUDIO_LLM_API_KEY (no paid request made)')
    base = (base_url or os.getenv('STUDIO_TTS_BASE_URL', 'https://api.openai.com/v1')).rstrip('/')
    if not base.startswith('https://') and not os.getenv('STUDIO_ALLOW_HTTP_LLM'):
        raise ConfigError('TTS endpoint must use HTTPS (or set STUDIO_ALLOW_HTTP_LLM=1 for local use)')
    return base


def _tts_request(base, key, model, voice, text):
    """Single TTS HTTP request; returns the raw audio bytes."""
    with httpx.Client(timeout=180) as client:
        response = client.post(base + '/audio/speech',
                               headers={'Authorization': f'Bearer {key}'},
                               json={'model': model, 'voice': voice, 'input': text,
                                     'response_format': 'mp3'})
        response.raise_for_status()
        return response.content


def generate_segment(payload):
    """Pure per-segment TTS call: request -> probe -> speed fit -> normalized WAV.

    No project, narration, or final-output writes happen here; only a single
    normalized segment WAV is produced under ``payload['segment_dir']``.
    """
    base = require_tts_config(payload.get('base_url'))
    key = os.getenv('STUDIO_TTS_API_KEY') or os.getenv('STUDIO_LLM_API_KEY')
    seq = payload['seq']
    desired = float(payload['duration'])
    seg_dir = Path(payload['segment_dir'])
    seg_dir.mkdir(parents=True, exist_ok=True)
    raw = seg_dir / f'{seq:03d}.mp3'
    normalized = seg_dir / f'{seq:03d}.wav'
    try:
        content = _tts_request(base, key, payload['model'], payload['voice'],
                               payload['text'])
    except _UNKNOWN_ERRORS as exc:
        raise UnknownRemote(str(exc)) from exc
    raw.write_bytes(content)
    actual = sound._probe(raw)
    filt = _speed_filter(actual, desired)
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n', '-i', str(raw),
                    '-af', f'{filt},aresample=48000,apad,atrim=duration={payload["duration"]},asetpts=PTS-STARTPTS',
                    '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', str(normalized)],
                   check=True)
    return {'file': str(normalized), 'duration': desired}


def synthesize(pid, voice='alloy', model='gpt-4o-mini-tts', live=False, max_chars=1200,
               force=False, *, budget_usd=None, request_id=None):
    if live: core.require_paid_budget(budget_usd)
    data = show(pid)
    lines = data['lines']; total = sum(len(x['text']) for x in lines)
    root = _root(pid); output = root/'narration.wav'
    if output.exists() and not force: raise ValueError('Narration exists; use --force to create a new version')
    if total > max_chars: raise ValueError(f'{total} characters exceeds --max-chars {max_chars}')
    if not live:
        return {'dry_run': True, 'model': model, 'voice': voice, 'characters': total,
                'requests': len(lines), 'output': str(output)}
    from . import tts_jobs, job_runner
    queued = tts_jobs.enqueue_batch(pid, lines, voice=voice, model=model,
                                    budget_usd=budget_usd, request_id=request_id,
                                    force=force)
    states = job_runner.run_until(queued['job_ids'])
    failed = [s for s in states if s['status'] != 'SUCCEEDED']
    if failed:
        detail = '; '.join(
            f"{s['id']}:{s['status'].lower()}"
            + (f":{s['block_reason']}" if s.get('block_reason') else '')
            for s in failed)
        raise RuntimeError(f'Narration TTS partial failure ({detail})')
    manifest = tts_jobs.aggregate(pid, states, lines=lines, force=force)
    manifest['run_id'] = queued['run_id']
    manifest['job_ids'] = queued['job_ids']
    return manifest


def attach(pid):
    root=_root(pid); audio=root/'narration.wav'; sub=root/'narration.srt'
    if not audio.is_file() or not sub.is_file(): raise ValueError('Generate narration first')
    try: existing=sound.config(pid)
    except ValueError: existing={}
    return sound.configure(pid,narration=str(audio),music=existing.get('music'),
        ambience=existing.get('ambience'),subtitles=str(sub),
        narration_gain=existing.get('narration_gain',1.0),
        music_gain=existing.get('music_gain',0.16),ambience_gain=existing.get('ambience_gain',0.20),
        fade_seconds=existing.get('fade_seconds',1.5))
