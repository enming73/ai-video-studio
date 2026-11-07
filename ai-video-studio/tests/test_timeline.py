from pathlib import Path
import pytest
from studio import core,timeline


def test_migration_and_selection(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path/'data')
    pid=core.create('a test',20,'mock',0)
    core.plan(pid,10)
    root=core.HOME/pid
    for seq in (1,2):
        orig=root/'clips'/f'{pid}-s{seq:03d}.mp4'
        orig.parent.mkdir(parents=True,exist_ok=True)
        orig.write_bytes(b'original'+bytes([seq]))
        i2v=root/'experiments'/f'i2v-{seq:03d}'/'clip.mp4'
        i2v.parent.mkdir(parents=True,exist_ok=True)
        i2v.write_bytes(b'i2v'+bytes([seq]))
        with core.connect() as db:
            db.execute('UPDATE shots SET file=?,status=? WHERE project_id=? AND seq=?',(str(orig),'COMPLETED',pid,seq))
    assert timeline.sync(pid)=={'original':2,'i2v':2}
    assert len(timeline.assets(pid))==4
    assert timeline.sync(pid)=={'original':2,'i2v':2}
    assert len(timeline.assets(pid))==4
    assert all(x['selected']['source']=='i2v' for x in timeline.timeline(pid))
    orig_id=next(x['id'] for x in timeline.assets(pid,1) if x['source']=='original')
    timeline.select(pid,1,orig_id)
    assert [x['selected']['source'] for x in timeline.timeline(pid)]==['original','i2v']
    with pytest.raises(ValueError): timeline.select(pid,2,orig_id)


def test_render_preserves_original(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path/'data')
    pid=core.create('a test',2,'mock',0)
    core.plan(pid,2)
    core.generate(pid)
    timeline.sync(pid)
    out=timeline.render(pid,source='original')
    assert out.is_file() and out.with_suffix('.json').is_file()
    core.verify_video(out,2)
    with pytest.raises(ValueError): timeline.render(pid,output=out,source='original')
    assert not (core.HOME/pid/'final.mp4').exists()
