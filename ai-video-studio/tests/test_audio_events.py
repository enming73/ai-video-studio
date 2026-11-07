import pytest
from studio import sound


def test_event_add_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(sound.core, 'HOME', tmp_path)
    monkeypatch.setattr(sound, '_duration', lambda pid: 60)
    monkeypatch.setattr(sound, '_probe', lambda p: 3)
    f=tmp_path/'bird.wav';f.write_bytes(b'fake')
    event=sound.audio_add('p',str(f),12.5,gain=.6,fade_in=.2,fade_out=.2)
    assert event['at']==12.5
    assert len(sound.audio_list('p'))==1
    with pytest.raises(ValueError): sound.audio_add('p',str(f),59,duration=3)
    assert sound.audio_remove('p',event['id'])==[]


def test_subtitle_font(monkeypatch):
    monkeypatch.setenv('STUDIO_SUBTITLE_FONT','Noto Sans CJK SC')
    monkeypatch.setenv('STUDIO_SUBTITLE_SIZE','24')
    assert 'FontName=Noto Sans CJK SC' in sound._subtitle_filter('/tmp/test.srt')
