import struct
import zlib
import pytest
from studio import core, references

def png(path):
    def chunk(tag, data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    raw=b'\x00\xff\x00\x00'  # one RGB pixel
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))

def test_reference_workflow(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path/'data')
    pid=core.create('A little elephant in a jungle',10,'mock',0)
    core.plan(pid,10)
    with core.connect() as db:
        db.execute('INSERT INTO storyboards VALUES (?,?,?,?)',(pid,'{"Luma": "gray elephant"}','test',core.now()))
    img=tmp_path/'test.png';png(img)
    c=references.import_character(pid,'Luma',img)
    s=references.import_shot(pid,1,img)
    assert c['sha256']==s['sha256']
    assert references.shot_reference_plan(pid)[0]['first_frame']['sha256']==s['sha256']
    assert references.validate_i2v(pid,1)['ready_for_i2v'] is False
    assert references.reference_manifest(pid)['characters']['Luma']==c
    with pytest.raises(ValueError): references.import_shot(pid,2,img)
    with pytest.raises(ValueError): references.import_character(pid,'../oops',img)

def test_bad_image(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'HOME',tmp_path/'data')
    pid=core.create('test',10,'mock',0)
    core.plan(pid)
    with core.connect() as db:
        db.execute('INSERT INTO storyboards VALUES (?,?,?,?)',(pid,'{}','test',core.now()))
    img=tmp_path/'bad.png';img.write_text('not an image')
    with pytest.raises(ValueError): references.import_character(pid,'Luma',img)
