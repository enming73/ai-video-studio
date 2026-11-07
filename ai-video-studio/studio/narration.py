"""v0.6.1: storyboard narration planning, TTS, synchronized shot-level SRT.

Paid calls are opt-in; original soundtrack/video and generated assets are preserved.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import uuid
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


def plan(pid, language='zh-CN', model=None, live=False, base_url=None):
    shots = _shots(pid)
    if not shots: raise ValueError('Project has no storyboard')
    target = _script_path(pid)
    if target.exists(): raise ValueError(f'Script already exists: {target}; edit it directly or back it up first')
    if not live:
        return {'dry_run': True, 'shots': len(shots), 'language': language, 'model': model, 'paid_request': bool(model)}
    if not model: raise ValueError('Provide --model to use the LLM')
    with core.connect() as db:
        p = dict(core.project(db,pid))
    prompt = {'story':p['prompt'],'language':language,'shots':[{'seq':s['seq'],'duration':s['duration'],'visual_prompt':s['prompt']} for s in shots]}
    data = core._llm_json(
        'Write a warm, concise narration for a short film. Return ONLY JSON with key "lines": '
        'an array of objects {"seq": integer, "text": string}. Each line must be spoken '
        'comfortably within its shot duration. Do not narrate camera directions. '
        'Keep characters and story consistent. Do not add facts absent from the visual story.',
        json.dumps(prompt,ensure_ascii=False),model,base_url or os.getenv('STUDIO_LLM_BASE_URL','https://api.openai.com/v1'))
    lines = data.get('lines')
    if not isinstance(lines,list) or len(lines)!=len(shots): raise ValueError('LLM returned wrong line count')
    by_seq = {}
    for item in lines:
        if not isinstance(item,dict) or not isinstance(item.get('seq'),int) or not isinstance(item.get('text'),str):
            raise ValueError('Invalid LLM narration line')
        if item['seq'] in by_seq or not item['text'].strip() or len(item['text'])>600:
            raise ValueError('Duplicate, empty, or too-long narration line')
        by_seq[item['seq']]=item['text'].strip()
    if set(by_seq)!={s['seq'] for s in shots}: raise ValueError('Narration sequence mismatch')
    result={'project_id':pid,'language':language,'model':model,
            'lines':[{'seq':s['seq'],'duration':s['duration'],'text':by_seq[s['seq']]} for s in shots]}
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'script':str(target),'lines':len(shots)}


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


def synthesize(pid, voice='alloy', model='gpt-4o-mini-tts', live=False, max_chars=1200, force=False):
    data=show(pid)
    lines=data['lines']; total=sum(len(x['text']) for x in lines)
    root=_root(pid); output=root/'narration.wav'; captions=root/'narration.srt'
    if output.exists() and not force: raise ValueError('Narration exists; use --force to create a new version')
    if total>max_chars: raise ValueError(f'{total} characters exceeds --max-chars {max_chars}')
    if not live: return {'dry_run':True,'model':model,'voice':voice,'characters':total,'requests':len(lines),'output':str(output)}
    load_dotenv()
    key=os.getenv('STUDIO_TTS_API_KEY') or os.getenv('STUDIO_LLM_API_KEY')
    if not key: raise ValueError('Set STUDIO_TTS_API_KEY or STUDIO_LLM_API_KEY')
    base=os.getenv('STUDIO_TTS_BASE_URL','https://api.openai.com/v1').rstrip('/')
    if not base.startswith('https://') and not os.getenv('STUDIO_ALLOW_HTTP_LLM'):
        raise ValueError('TTS endpoint must use HTTPS')
    run_dir=root / ('run-'+uuid.uuid4().hex[:10]);run_dir.mkdir()
    segments=[]
    with httpx.Client(timeout=180) as client:
        for line in lines:
            seq=line['seq']; raw=run_dir/f'{seq:03d}.mp3'; normalized=run_dir/f'{seq:03d}.wav'
            response=client.post(base+'/audio/speech',headers={'Authorization':f'Bearer {key}'},
                json={'model':model,'voice':voice,'input':line['text'],'response_format':'mp3'})
            if response.is_error:
                raise RuntimeError(f'TTS HTTP {response.status_code}: {response.text[:1000]}')
            raw.write_bytes(response.content)
            actual=sound._probe(raw)
            filt=_speed_filter(actual,float(line['duration']))
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-i',str(raw),
                '-af',f'{filt},aresample=48000,apad,atrim=duration={line["duration"]},asetpts=PTS-STARTPTS',
                '-ar','48000','-ac','2','-c:a','pcm_s16le',str(normalized)],check=True)
            segments.append(normalized)
    # Concat normalized WAV segments with explicit concat demuxer.
    concat=run_dir/'concat.txt'
    concat.write_text(''.join("file '"+str(p.resolve()).replace("'", "'\\''")+"'\n" for p in segments),encoding='utf-8')
    temp=run_dir/'combined.wav'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-f','concat','-safe','0',
                    '-i',str(concat),'-c:a','pcm_s16le',str(temp)],check=True)
    duration=sum(float(x['duration']) for x in lines)
    if abs(sound._probe(temp)-duration)>0.15: raise RuntimeError('Combined narration duration mismatch')
    if output.exists():
        output.rename(root/('narration-backup-'+uuid.uuid4().hex[:8]+'.wav'))
    if captions.exists():
        captions.rename(root/('narration-backup-'+uuid.uuid4().hex[:8]+'.srt'))
    temp.replace(output);_srt(lines,captions)
    manifest={'project_id':pid,'model':model,'voice':voice,'characters':total,'duration':duration,
              'audio':str(output),'subtitles':str(captions),'segments':len(segments)}
    (root/'narration-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
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
