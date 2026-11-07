import subprocess
from pathlib import Path
import pytest
from studio import core, sound

@pytest.fixture
def project(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path/'studio-data')
    pid=core.create('A small story',6,'mock',0)
    core.plan(pid,3)
    return pid,tmp_path

def _media(path,kind):
    if kind=='video':
        cmd=['ffmpeg','-loglevel','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=24:d=6','-c:v','libx264','-pix_fmt','yuv420p',str(path)]
    else:
        cmd=['ffmpeg','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440:duration=2','-c:a','pcm_s16le',str(path)]
    subprocess.run(cmd,check=True)

def test_config_and_draft(project):
    pid,tmp=project
    draft=sound.subtitles_from_shots(pid)
    assert '00:00:03,000' in draft.read_text()
    assert '[镜头 2' in draft.read_text()
    with pytest.raises(ValueError): sound.subtitles_from_shots(pid)
    audio=tmp/'voice.wav';_media(audio,'audio')
    cfg=sound.configure(pid,narration=str(audio),subtitles=str(draft))
    assert cfg['narration']==str(audio.resolve())
    assert sound.config(pid)['subtitles']==str(draft)

def test_export_audio_and_subtitles(project):
    pid,tmp=project
    video=tmp/'input.mp4';audio=tmp/'music.wav'
    _media(video,'video');_media(audio,'audio')
    captions=sound.subtitles_from_shots(pid)
    sound.configure(pid,music=str(audio),subtitles=str(captions),fade_seconds=1)
    out=sound.export(pid,video=str(video))
    assert out.exists() and abs(sound._probe(out)-6)<0.5
    with pytest.raises(ValueError): sound.export(pid,video=str(video),output=str(out))
    out2=sound.export(pid,video=str(video),burn_subtitles=True)
    assert out2.exists() and abs(sound._probe(out2)-6)<0.5

def test_invalid_config(project):
    pid,_=project
    with pytest.raises(ValueError): sound.configure(pid,music='/does/not/exist')
    with pytest.raises(ValueError): sound.configure(pid,fade_seconds=4)
