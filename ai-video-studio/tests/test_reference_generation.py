import base64, json
from pathlib import Path
import pytest
from studio import core, references, reference_generation as rg

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aZkAAAAASUVORK5CYII=')

def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path)
    pid=core.create('Luma the elephant',10,'minimax',5)
    with core.connect() as db:
        db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)', (pid+'-s001',pid,1,'Luma walks in jungle',10,'PLANNED'))
        db.execute('INSERT INTO storyboards(project_id,bible_json,planner,updated) VALUES (?,?,?,?)',(pid,json.dumps({'Luma':'gray elephant'}),'test','now'))
    f=tmp_path/'ref.png';f.write_bytes(PNG)
    references.import_character(pid,'Luma',f)
    return pid,f

def test_frame_dry_run(tmp_path,monkeypatch):
    pid,_=setup(tmp_path,monkeypatch)
    result=rg.generate_frame(pid,1)
    assert result['dry_run'] and result['characters']==['Luma']
    assert references.reference_manifest(pid)['shots']=={}

def test_i2v_dry_run_and_no_mutation(tmp_path,monkeypatch):
    pid,f=setup(tmp_path,monkeypatch)
    references.import_shot(pid,1,f)
    payload=rg.i2v_plan(pid,1)
    assert payload['content'][1]['role']=='first_frame'
    assert payload['ratio']=='adaptive'
    assert rg.i2v(pid,1)['estimated_video_usd']==0.8
    assert not (tmp_path/pid/'experiments').exists()

def test_i2v_paid_requires_ceiling(tmp_path,monkeypatch):
    pid,f=setup(tmp_path,monkeypatch);references.import_shot(pid,1,f)
    with pytest.raises(ValueError,match='max-usd'):
        rg.i2v(pid,1,live=True)
