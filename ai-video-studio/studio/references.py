"""v0.4 reference-first workflow. Offline by default; no unverified paid APIs."""
from __future__ import annotations
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from . import core

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}

def _asset_root(pid):
    with core.connect() as db:
        core.project(db, pid)
    return core.HOME / pid / 'references'

def _safe_image(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError('Reference must be an existing PNG/JPG/WEBP image')
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('Reference image exceeds 20 MB')
    signature = path.read_bytes()[:16]
    if not (signature.startswith(b'\x89PNG\r\n\x1a\n') or signature.startswith(b'\xff\xd8\xff') or signature.startswith(b'RIFF') and signature[8:12] == b'WEBP'):
        raise ValueError('Invalid image file signature')
    return path

def _store(pid, path, folder, label):
    source = _safe_image(path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    directory = _asset_root(pid) / folder
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{label}-{digest[:16]}{source.suffix.lower()}'
    if not target.exists():
        shutil.copyfile(source, target)
    return str(target), digest

def characters(pid):
    bible = core.storyboard(pid)['character_bible']
    if bible is None:
        raise ValueError('Run smart-plan first to create a Character Bible')
    return bible

def reference_manifest(pid):
    root = _asset_root(pid)
    manifest = root / 'manifest.json'
    if not manifest.exists():
        return {'project_id': pid, 'characters': {}, 'shots': {}}
    return json.loads(manifest.read_text(encoding='utf-8'))

def _save(pid, data):
    target = _asset_root(pid) / 'manifest.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.part')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(target)

def import_character(pid, name, path):
    if not name.strip() or '/' in name or '\\' in name or name in ('.','..'):
        raise ValueError('Invalid character name')
    characters(pid)
    stored, digest = _store(pid, path, 'characters', 'character')
    data = reference_manifest(pid)
    data['characters'][name] = {'file': stored, 'sha256': digest}
    _save(pid, data)
    return data['characters'][name]

def import_shot(pid, seq, path):
    shots = core.storyboard(pid)['shots']
    shot = next((s for s in shots if s['seq'] == seq), None)
    if shot is None:
        raise ValueError('Shot does not exist')
    stored, digest = _store(pid, path, 'shots', f'shot-{seq:03d}')
    data = reference_manifest(pid)
    data['shots'][str(seq)] = {'file': stored, 'sha256': digest, 'shot_id': shot['id']}
    _save(pid, data)
    return data['shots'][str(seq)]

def shot_reference_plan(pid):
    board = core.storyboard(pid)
    refs = reference_manifest(pid)
    result = []
    for shot in board['shots']:
        result.append({'seq': shot['seq'], 'shot_id': shot['id'], 'prompt': shot['prompt'],
                       'character_references': refs['characters'],
                       'first_frame': refs['shots'].get(str(shot['seq'])),
                       'status': shot['status']})
    return result

def extract_keyframes(pid, seq, count=3):
    if not 1 <= count <= 12:
        raise ValueError('count must be 1..12')
    shot = next((s for s in core.storyboard(pid)['shots'] if s['seq'] == seq), None)
    if not shot or not shot['file'] or not Path(shot['file']).is_file():
        raise ValueError('Shot has no local video asset')
    core.require_ffmpeg()
    output = _asset_root(pid) / 'keyframes' / f'{seq:03d}'
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        at = shot['duration'] * (i + 1) / (count + 1)
        path = output / f'frame-{i+1:02d}.png'
        core.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(at),'-i',shot['file'],
                  '-frames:v','1',str(path)])
        paths.append(str(path))
    return paths

def validate_i2v(pid, seq):
    """Prevent accidental paid text-to-video calls disguised as image-to-video."""
    item = next((s for s in shot_reference_plan(pid) if s['seq'] == seq), None)
    if not item:
        raise ValueError('Unknown shot')
    if not item['first_frame']:
        raise ValueError('Import an approved shot first frame before I2V')
    if item['status'] not in ('PLANNED','FAILED'):
        raise ValueError('Existing generated/submitted shot is immutable; use a new project')
    return {'ready_for_i2v': False, 'first_frame': item['first_frame']['file'],
            'reason': 'MiniMax H3 image-to-video payload and model capability are not verified in this release; no paid request sent'}
