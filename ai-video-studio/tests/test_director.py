import os
import subprocess
from pathlib import Path
import pytest
from studio import core, timeline, director


def test_director_offline(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path)
    pid=core.create('A little elephant in the forest',5,'mock',0)
    core.plan(pid,5)
    clip=tmp_path/'clip.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=640x360:r=5',
                    '-t','5','-c:v','mpeg4','-y',str(clip)],check=True)
    aid=timeline.import_asset(pid,1,str(clip))
    timeline.select(pid,1,aid)
    result=director.analyze(pid,frames=2,black=False,freeze=False)
    assert result['paid_api_calls']==0
    assert len(result['shots'][0]['frames'])==2
    assert result['shots'][0]['status']=='technical_pass'
    assert director.report(pid,result['report_id'])['report_id']==result['report_id']
    assert len(director.reports(pid))==1
    assert clip.exists()


def test_missing_asset(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path)
    pid=core.create('A story',5,'mock',0)
    core.plan(pid,5)
    result=director.analyze(pid,frames=1)
    assert result['shots'][0]['status']=='missing'
    with pytest.raises(ValueError): director.analyze(pid,frames=0)
