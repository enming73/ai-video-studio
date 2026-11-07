"""v0.6 non-destructive audio, subtitles and final export. No paid API calls."""
from __future__ import annotations
import json
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
    return "subtitles=filename='"+value+"'"


def export(pid, video=None, output=None, source='selected', burn_subtitles=False):
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
    for key,gain in [('narration','narration_gain'),('music','music_gain'),('ambience','ambience_gain')]:
        if not cfg.get(key): continue
        path=_valid_file(cfg[key]);inputs+=['-stream_loop','-1' if key!='narration' else '0','-i',str(path)]
        fade=float(cfg['fade_seconds'])
        af=f'[{idx}:a]aresample=48000,volume={float(cfg[gain])},atrim=duration={duration},asetpts=PTS-STARTPTS'
        if key!='narration' and fade>0:
            af+=f',afade=t=in:st=0:d={fade},afade=t=out:st={max(0,duration-fade)}:d={fade}'
        label=f'a{idx}';filters.append(af+f'[{label}]');audio_labels.append(f'[{label}]');idx+=1
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
                  'config':cfg,'burn_subtitles':burn_subtitles,'duration':duration}
        out.with_suffix('.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        return out
    except Exception:
        out.unlink(missing_ok=True)
        raise
