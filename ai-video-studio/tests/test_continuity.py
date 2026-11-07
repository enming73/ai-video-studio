from pathlib import Path
from studio import continuity
import pytest

def test_invalid_report_id():
    with pytest.raises(ValueError):
        continuity._valid_id('../secrets')
    assert continuity._valid_id('abcdef123456') == 'abcdef123456'

def test_boundary_report_offline(tmp_path, monkeypatch):
    from studio import core, director, timeline
    monkeypatch.setattr(core,'HOME',tmp_path)
    monkeypatch.setattr(core,'storyboard',lambda pid:{'shots':[{'seq':1,'prompt':'two characters','duration':10},{'seq':2,'prompt':'frog enters','duration':10}]})
    video=tmp_path/'video.mp4';video.write_bytes(b'fake')
    monkeypatch.setattr(timeline,'timeline',lambda pid:[{'seq':1,'selected':{'id':'a','file':str(video)}},{'seq':2,'selected':{'id':'b','file':str(video)}}])
    monkeypatch.setattr(director,'_probe',lambda path:{'duration':10.0})
    def fake_run(args):
        Path(args[-1]).write_bytes(b'jpeg')
    monkeypatch.setattr(director,'_run',fake_run)
    result=continuity.analyze('demo')
    assert result['paid_api_calls']==0
    assert len(result['boundaries'])==1
    assert len(result['boundaries'][0]['frames']['out'])==2
    assert len(result['boundaries'][0]['frames']['in'])==2
    assert (tmp_path/'demo'/'continuity-reports'/result['report_id']/'report.json').exists()
