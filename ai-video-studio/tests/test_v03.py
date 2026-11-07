from pathlib import Path
import pytest
from studio import core

@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return tmp_path

def test_smart_plan_and_preserve(local, monkeypatch):
    pid = core.create('A gray elephant in a jungle', 20, 'mock', 0)
    def fake_llm(*args):
        return {'character_bible': {'hero':'gray elephant'}, 'shots': [
            {'prompt':'The same gray elephant wakes up beneath the jungle canopy.'},
            {'prompt':'The same gray elephant walks slowly toward a bright river.'}]}
    monkeypatch.setattr(core, '_llm_json', fake_llm)
    assert core.smart_plan(pid,10,'fake-model') == 2
    assert core.storyboard(pid)['character_bible']['hero'] == 'gray elephant'
    assert 'wakes up' in core.storyboard(pid)['shots'][0]['prompt']
    with pytest.raises(ValueError): core.smart_plan(pid,10,'fake-model')
    assert core.update_shot(pid,2,'The elephant splashes water under warm sunlight.')
    path = core.save_storyboard(pid,local/'storyboard.json')
    assert path.exists()

def test_invalid_llm_is_atomic(local,monkeypatch):
    pid=core.create('Elephant',20,'mock',0)
    monkeypatch.setattr(core,'_llm_json',lambda *a: {'shots': []})
    with pytest.raises(ValueError): core.smart_plan(pid,10,'fake')
    assert core.storyboard(pid)['shots']==[]
