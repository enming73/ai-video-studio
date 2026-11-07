import json
import pytest
from studio import sound

def test_edit_audio_persists_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(sound, '_duration', lambda pid: 60)
    monkeypatch.setattr(sound, '_events_path', lambda pid: tmp_path / 'audio-events.json')
    f=tmp_path/'audio-events.json'
    f.write_text(json.dumps([{'id':'abc','file':'test.wav','at':5,'duration':10,'gain':0.5,'fade_in':1,'fade_out':1}]))
    result=sound.audio_update('p','abc',at=12.5,duration=8,gain=0.7)
    assert result['at']==12.5
    assert json.loads(f.read_text())[0]['duration']==8
    with pytest.raises(ValueError):sound.audio_update('p','abc',at=59)
    assert json.loads(f.read_text())[0]['at']==12.5
    with pytest.raises(ValueError):sound.audio_update('p','missing',gain=1)
    with pytest.raises(ValueError):sound.audio_update('p','abc',duration=1)
