"""v0.6 non-destructive audio, subtitles and final export. No paid API calls."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from . import core, timeline


def _probe(path):
    result = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                             '-of','default=noprint_wrappers=1:nokey=1',str(path)],
                            capture_output=True,text=True,check=True)
    return float(result.stdout.strip())


def _valid_file(file):
    if file is None: return None
    p=Path(file).expanduser().resolve()
    if not p.is_file() or p.stat().st_size==0: raise ValueError(f'Missing or empty media file: {p}')
    return p


def _duration(pid):
    with core.connect() as db:
        return int(core.project(db,pid)['duration'])


def _config_path(pid):
    return core.HOME/pid/'soundtrack.json'


def configure(pid, narration=None, music=None, ambience=None, subtitles=None,
              narration_gain=1.0, music_gain=0.16, ambience_gain=0.20,
              fade_seconds=1.5):
    duration=_duration(pid)
    if not 0<=fade_seconds<=duration/2: raise ValueError('Invalid fade duration')
    for v in (narration_gain,music_gain,ambience_gain):
        if not 0<=v<=3: raise ValueError('Gains must be between 0 and 3')
    paths={k:_valid_file(v) for k,v in {'narration':narration,'music':music,'ambience':ambience,'subtitles':subtitles}.items()}
    if paths['subtitles'] and paths['subtitles'].suffix.lower()!='.srt':
        raise ValueError('Subtitles must be an .srt file')
    config={'project_id':pid,'narration':str(paths['narration']) if paths['narration'] else None,
            'music':str(paths['music']) if paths['music'] else None,
            'ambience':str(paths['ambience']) if paths['ambience'] else None,
            'subtitles':str(paths['subtitles']) if paths['subtitles'] else None,
            'narration_gain':narration_gain,'music_gain':music_gain,
            'ambience_gain':ambience_gain,'fade_seconds':fade_seconds}
    target=_config_path(pid); target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    return config


def config(pid):
    _duration(pid)
    path=_config_path(pid)
    if not path.is_file(): raise ValueError('No soundtrack configured; run studio soundtrack-set')
    return json.loads(path.read_text(encoding='utf-8'))


def _events_path(pid):
    return core.HOME/pid/'audio-events.json'


def audio_list(pid):
    _duration(pid)
    path=_events_path(pid)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def audio_add(pid, file, at, duration=None, gain=1.0, fade_in=0.0, fade_out=0.0):
    total=_duration(pid)
    path=_valid_file(file)
    at=float(at)
    if not 0<=at<total: raise ValueError('Event start must be within the project')
    if duration is None: duration=min(_probe(path),total-at)
    duration=float(duration)
    if not 0<duration<=total-at+0.001: raise ValueError('Event exceeds project duration')
    if not 0<=gain<=3: raise ValueError('Gain must be between 0 and 3')
    if min(fade_in,fade_out)<0 or fade_in+fade_out>duration: raise ValueError('Invalid event fades')
    events=audio_list(pid)
    event={'id':uuid.uuid4().hex[:10],'file':str(path),'at':at,'duration':duration,
           'gain':float(gain),'fade_in':float(fade_in),'fade_out':float(fade_out)}
    events.append(event)
    target=_events_path(pid);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(events,indent=2,ensure_ascii=False),encoding='utf-8')
    return event


def audio_remove(pid, event_id):
    events=audio_list(pid)
    remaining=[e for e in events if e['id']!=event_id]
    if len(remaining)==len(events): raise ValueError('Unknown audio event')
    _events_path(pid).write_text(json.dumps(remaining,indent=2,ensure_ascii=False),encoding='utf-8')
    return remaining


def _srt_timestamp(sec):
    ms=round(sec*1000)
    h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def subtitles_from_shots(pid, output=None):
    with core.connect() as db:
        core.project(db,pid)
        shots=db.execute('SELECT seq,duration,prompt FROM shots WHERE project_id=? ORDER BY seq',(pid,)).fetchall()
    if not shots: raise ValueError('No shots')
    target=Path(output).expanduser().resolve() if output else core.HOME/pid/'shot-captions.srt'
    if target.exists(): raise ValueError(f'File exists: {target}')
    target.parent.mkdir(parents=True,exist_ok=True)
    start=0; lines=[]
    for i,shot in enumerate(shots,1):
        end=start+shot['duration']
        # Draft captions only. Prompts are NOT a spoken transcript.
        lines.extend([str(i),f'{_srt_timestamp(start)} --> {_srt_timestamp(end)}',
                      f'[镜头 {shot["seq"]}：请填写字幕]', ''])
        start=end
    target.write_text('\n'.join(lines),encoding='utf-8')
    return target


def _subtitle_filter(path):
    # FFmpeg filtergraph escaping: path is quoted for subtitles filename option.
    value=str(path).replace('\\','\\\\').replace(':','\\:').replace("'", "\\'").replace('[','\\[').replace(']','\\]').replace(',','\\,')
    font=os.getenv('STUDIO_SUBTITLE_FONT','Noto Sans CJK SC')
    size=int(os.getenv('STUDIO_SUBTITLE_SIZE','24'))
    if not 8<=size<=96: raise ValueError('Invalid subtitle font size')
    if any(c in font for c in "',:;\\[]"): raise ValueError('Invalid subtitle font name')
    return "subtitles=filename='"+value+"':force_style='FontName="+font+",FontSize="+str(size)+",Outline=2,Shadow=1'"


def export(pid, video=None, output=None, source='selected', burn_subtitles=False, ducking=False):
    """Use an existing timeline render or build a new one; never overwrite input/output."""
    duration=_duration(pid)
    cfg=config(pid)
    root=core.HOME/pid
    source_video=_valid_file(video) if video else None
    if source_video is None:
        source_video=timeline.render(pid,source=source)
    if abs(_probe(source_video)-duration)>0.5:
        raise ValueError(f'Video duration differs from project: {source_video}')
    out=Path(output).expanduser().resolve() if output else root/f'final-sound-{uuid.uuid4().hex[:8]}.mp4'
    if out.exists(): raise ValueError(f'Output exists; refusing overwrite: {out}')
    if out==source_video: raise ValueError('Output must differ from source video')
    out.parent.mkdir(parents=True,exist_ok=True)
    inputs=['-i',str(source_video)]
    filters=[];audio_labels=[];idx=1
    tracks={}
    for key,gain in [('narration','narration_gain'),('music','music_gain'),('ambience','ambience_gain')]:
        if not cfg.get(key): continue
        path=_valid_file(cfg[key])
        inputs+=['-stream_loop','-1' if key!='narration' else '0','-i',str(path)]
        fade=float(cfg['fade_seconds'])
        af=f'[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={float(cfg[gain])},atrim=duration={duration},asetpts=PTS-STARTPTS'
        if key!='narration' and fade>0:
            af+=f',afade=t=in:st=0:d={fade},afade=t=out:st={max(0,duration-fade)}:d={fade}'
        label=f'a{idx}';filters.append(af+f'[{label}]');tracks[key]=label;idx+=1
    if ducking and 'music' in tracks and 'narration' in tracks:
        # Split narration: one copy drives sidechain compression, one stays audible.
        filters.append(f'[{tracks["narration"]}]asplit=2[narration_main][narration_side]')
        filters.append(f'[{tracks["music"]}][narration_side]sidechaincompress=threshold=0.03:ratio=8:attack=25:release=400[music_ducked]')
        tracks['narration']='narration_main'
        tracks['music']='music_ducked'
    audio_labels.extend(f'[{v}]' for v in tracks.values())
    events=audio_list(pid)
    for event in events:
        path=_valid_file(event['file'])
        at=float(event['at']);length=float(event['duration'])
        if not 0<=at<duration or length<=0 or at+length>duration+0.001:
            raise ValueError(f'Invalid audio event timing: {event["id"]}')
        gain=float(event['gain']);fi=float(event.get('fade_in',0));fo=float(event.get('fade_out',0))
        if not 0<=gain<=3 or min(fi,fo)<0 or fi+fo>length:
            raise ValueError(f'Invalid audio event gain/fade: {event["id"]}')
        inputs+=['-i',str(path)]
        af=f'[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain},atrim=duration={length},asetpts=PTS-STARTPTS'
        if fi: af+=f',afade=t=in:st=0:d={fi}'
        if fo: af+=f',afade=t=out:st={length-fo}:d={fo}'
        af+=f',adelay={round(at*1000)}:all=1[e{idx}]'
        filters.append(af);audio_labels.append(f'[e{idx}]');idx+=1
    if audio_labels:
        if len(audio_labels)>1:
            filters.append(''.join(audio_labels)+f'amix=inputs={len(audio_labels)}:duration=longest:normalize=0,atrim=duration={duration}[audio]')
        else:
            filters.append(f'{audio_labels[0]}anull[audio]')
    else:
        filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration}[audio]')
    args=['ffmpeg','-hide_banner','-loglevel','error','-n',*inputs]
    if burn_subtitles:
        sub=_valid_file(cfg.get('subtitles'))
        if sub is None: raise ValueError('No .srt subtitles configured')
        args+=['-vf',_subtitle_filter(sub),'-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p']
    else:
        args+=['-c:v','copy']
    args+=['-filter_complex',';'.join(filters),'-map','0:v:0','-map','[audio]',
           '-c:a','aac','-b:a','192k','-t',str(duration),'-movflags','+faststart',str(out)]
    try:
        core.run(args)
        if abs(_probe(out)-duration)>0.5: raise RuntimeError('Unexpected output duration')
        manifest={'project_id':pid,'video':str(source_video),'output':str(out),
                  'config':cfg,'audio_events':events,'ducking':ducking,'burn_subtitles':burn_subtitles,'duration':duration}
        out.with_suffix('.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        return out
    except Exception:
        out.unlink(missing_ok=True)
        raise
