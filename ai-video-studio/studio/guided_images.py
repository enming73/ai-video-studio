"""Project-scoped immutable image uploads and generated versions."""
import hashlib
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4
from . import core, guided_characters, guided_workflow
import httpx
from dotenv import load_dotenv

MAX_IMAGE_BYTES = 20 * 1024 * 1024
_FORMATS = {'.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff', '.webp': b'RIFF'}


def _safe_path(pid, image_id, suffix):
    root = (core.HOME / pid).resolve()
    if not root.is_relative_to(core.HOME.resolve()):
        raise ValueError('Invalid project path')
    directory = root / 'guided-images'
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root):
        raise ValueError('Image directory outside project')
    return directory / (image_id + suffix)


def _validate_filename(filename: str) -> str:
    if not isinstance(filename, str) or filename != Path(filename).name or '\\' in filename:
        raise ValueError('Invalid image filename')
    suffix = Path(filename).suffix.lower()
    if suffix not in _FORMATS:
        raise ValueError('Unsupported image format')
    return suffix


def _validated_content(content: bytes, suffix: str) -> None:
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_IMAGE_BYTES:
        raise ValueError('Unsupported image or size exceeds 20 MiB')
    if not content.startswith(_FORMATS[suffix]) or (suffix == '.webp' and content[8:12] != b'WEBP'):
        raise ValueError('Image signature does not match format')


def _check_character(db, pid, character_id):
    guided_characters._enabled(db, pid)
    if not db.execute('SELECT 1 FROM guided_characters WHERE id=? AND project_id=?', (character_id, pid)).fetchone():
        raise ValueError('Unknown character')


def _register_upload(pid, character_id, suffix, content, revision):
    with core.connect() as db:
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _check_character(db, pid, character_id)
    image_id = uuid4().hex
    target = _safe_path(pid, image_id, suffix)
    created = False
    try:
        with target.open('xb') as f:
            created = True
            f.write(content)
        try:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(target), '-frames:v', '1', '-f', 'null', '-'],
                                    capture_output=True, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ValueError('FFmpeg is required to validate image content') from exc
        if result.returncode:
            raise ValueError('Invalid image content')
        digest = hashlib.sha256(content).hexdigest()
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            _check_character(db, pid, character_id)
            db.execute('INSERT INTO guided_character_images VALUES (?,?,?,?,?,?,?)',
                       (image_id, pid, character_id, str(target), digest, 'upload', core.now()))
            guided_characters._changed(db, pid)
        return {'id': image_id, 'character_id': character_id, 'sha256': digest, 'source': 'upload', 'revision': revision + 1}
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def import_character_image(pid: str, character_id: str, filename: str, content: bytes, revision: int) -> dict:
    suffix = _validate_filename(filename)
    _validated_content(content, suffix)
    return _register_upload(pid, character_id, suffix, content, revision)


def import_character_image_file(pid: str, character_id: str, filename: str, path: Path, revision: int) -> dict:
    """Validate a fully streamed temp file, then move it without buffering in memory."""
    suffix = _validate_filename(filename)
    size = path.stat().st_size
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError('Unsupported image or size exceeds 20 MiB')
    with path.open('rb') as f:
        head = f.read(12)
    if not head.startswith(_FORMATS[suffix]) or (suffix == '.webp' and head[8:12] != b'WEBP'):
        raise ValueError('Image signature does not match format')
    with core.connect() as db:
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _check_character(db, pid, character_id)
    try:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-frames:v', '1', '-f', 'null', '-'],
                                capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError('FFmpeg is required to validate image content') from exc
    if result.returncode:
        raise ValueError('Invalid image content')
    image_id = uuid4().hex
    target = _safe_path(pid, image_id, suffix)
    moved = False
    try:
        os.replace(path, target)
        moved = True
        digest = hashlib.sha256()
        with target.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        digest = digest.hexdigest()
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            _check_character(db, pid, character_id)
            db.execute('INSERT INTO guided_character_images VALUES (?,?,?,?,?,?,?)',
                       (image_id, pid, character_id, str(target), digest, 'upload', core.now()))
            guided_characters._changed(db, pid)
        return {'id': image_id, 'character_id': character_id, 'sha256': digest, 'source': 'upload', 'revision': revision + 1}
    except Exception:
        if moved:
            target.unlink(missing_ok=True)
        raise


def list_character_images(pid: str, character_id: str) -> list[dict]:
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        if not db.execute('SELECT 1 FROM guided_characters WHERE id=? AND project_id=?', (character_id, pid)).fetchone():
            raise ValueError('Unknown character')
        return [dict(r) for r in db.execute('SELECT id,character_id,sha256,source,created FROM guided_character_images WHERE project_id=? AND character_id=? ORDER BY created,id', (pid, character_id))]


def image_path(pid: str, image_id: str) -> Path:
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        row = db.execute('SELECT file,sha256 FROM guided_character_images WHERE project_id=? AND id=? UNION ALL SELECT file,sha256 FROM guided_frame_versions WHERE project_id=? AND id=? UNION ALL SELECT file,sha256 FROM guided_boundary_versions WHERE project_id=? AND id=?', (pid, image_id, pid, image_id, pid, image_id)).fetchone()
        if not row:
            raise ValueError('Unknown image')
    path = Path(row['file']).resolve()
    root = (core.HOME / pid / 'guided-images').resolve()
    if not root.is_relative_to((core.HOME / pid).resolve()) or not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Image unavailable')
    if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('Image changed')
    return path


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)],
                                capture_output=True, check=True, timeout=15)
        stream = json.loads(result.stdout)['streams'][0]
        return int(stream['width']), int(stream['height'])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as exc:
        raise ValueError('Could not verify image dimensions') from exc


def crop_to_16x9(source: Path, output: Path) -> bool:
    """Crop to a centered 16:9 frame at 1536x864, returning whether a crop happened."""
    width, height = image_dimensions(source)
    ratio = 16 / 9
    if abs(width / height - ratio) <= 0.01:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        return False
    if width / height > ratio:
        crop_w = int(height * ratio)
        crop_h = height
    else:
        crop_w = width
        crop_h = int(width / ratio)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    x = (width - crop_w) // 2
    y = (height - crop_h) // 2
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),
                                 '-vf',f'crop={crop_w}:{crop_h}:{x}:{y},scale=1536:864',
                                 '-frames:v','1',str(output)], capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError('FFmpeg is required to normalize image aspect ratio') from exc
    if result.returncode:
        raise ValueError('Could not normalize image aspect ratio')
    verify = subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(output),'-frames:v','1','-f','null','-'],
                            capture_output=True, timeout=15)
    if verify.returncode:
        raise ValueError('Could not normalize image aspect ratio')
    return True


def preview_character(pid: str, character_id: str, model: str) -> dict:
    if model != 'gpt-image-2.5-sunburst':
        raise ValueError('Image model capability is unverified')
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        role = db.execute('SELECT * FROM guided_characters WHERE project_id=? AND id=?', (pid, character_id)).fetchone()
        if not role: raise ValueError('Unknown character')
        prompt = f"Character reference portrait for {role['name']}. {role['description']}. One character, no text."
    return {'character_id': character_id, 'model': model, 'prompt': prompt,
            'estimated_cost_usd': None, 'note': 'Budget is not a provider-enforced spending cap.'}


def _image_request(url, key, payload):
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        response = client.post(url, headers={'Authorization': 'Bearer ' + key}, json=payload)
        response.raise_for_status()
        return response.json()


def image_attempt(pid: str, attempt_id: str) -> dict:
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        row = db.execute('SELECT id,target_id,kind,request_id,model,budget,status,image_id,error FROM guided_image_attempts WHERE project_id=? AND id=?', (pid, attempt_id)).fetchone()
        if not row: raise ValueError('Unknown image attempt')
        return dict(row)


def generate_character(pid: str, character_id: str, model: str, budget_usd: float, *, confirm_paid: bool, request_id: str, revision: int) -> dict:
    details = preview_character(pid, character_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    load_dotenv()
    key = os.getenv('STUDIO_IMAGE_API_KEY') or os.getenv('OPENAI_API_KEY')
    base = os.getenv('STUDIO_IMAGE_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    if not key or not base.startswith('https://'):
        raise ValueError('Image API key and HTTPS endpoint required')
    aid = uuid4().hex
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM guided_image_attempts WHERE project_id=? AND request_id=?', (pid, request_id)).fetchone()
        if existing:
            if existing['target_id'] != character_id or existing['kind'] != 'character':
                raise ValueError('Request ID belongs to another image operation')
            return {k: existing[k] for k in ('id','target_id','kind','request_id','model','budget','status','image_id','error')}
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        if db.execute("SELECT 1 FROM guided_image_attempts WHERE project_id=? AND target_id=? AND kind='character' AND status IN ('SUBMITTING','UNKNOWN')", (pid, character_id)).fetchone():
            raise ValueError('Previous image submission is unresolved')
        role = db.execute('SELECT name,description FROM guided_characters WHERE project_id=? AND id=?', (pid, character_id)).fetchone()
        if not role or f"Character reference portrait for {role['name']}. {role['description']}. One character, no text." != details['prompt']:
            raise ValueError('Character changed after preview')
        db.execute('INSERT INTO guided_image_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, pid, character_id, 'character', request_id, model, budget_usd, 'SUBMITTING', json.dumps(details), None, None))
    try:
        data = _image_request(base + '/images/generations', key, {'model': model, 'prompt': details['prompt'], 'output_format': 'png'})
        raw = base64.b64decode(data['data'][0]['b64_json'], validate=True)
        if not raw.startswith(_FORMATS['.png']) or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError('Provider returned an invalid image')
        image_id = uuid4().hex
        target = _safe_path(pid, image_id, '.png')
        with target.open('xb') as f: f.write(raw)
        result = subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(target),'-frames:v','1','-f','null','-'],capture_output=True,timeout=15)
        if result.returncode: raise ValueError('Provider returned an invalid image')
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO guided_character_images VALUES (?,?,?,?,?,?,?)',
                       (image_id, pid, character_id, str(target), hashlib.sha256(raw).hexdigest(), 'generated', core.now()))
            db.execute("UPDATE guided_image_attempts SET status='COMPLETED',image_id=? WHERE project_id=? AND id=?", (image_id, pid, aid))
            guided_characters._changed(db, pid)
    except Exception:
        with core.connect() as db:
            db.execute("UPDATE guided_image_attempts SET status='UNKNOWN',error=? WHERE project_id=? AND id=?", ('Image request failed or outcome unknown; check provider charges.', pid, aid))
    return image_attempt(pid, aid)


def _image_edit(url, key, payload, files):
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        response = client.post(url, headers={'Authorization': 'Bearer ' + key}, data=payload, files=files)
        response.raise_for_status()
        return response.json()


def resolve_attempt(pid: str, attempt_id: str, *, confirm_checked: bool) -> dict:
    if confirm_checked is not True: raise ValueError('Confirm that provider charges were checked')
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        result = db.execute("UPDATE guided_image_attempts SET status='ACKNOWLEDGED' WHERE project_id=? AND id=? AND status IN ('SUBMITTING','UNKNOWN')", (pid, attempt_id))
        if result.rowcount != 1: raise ValueError('No unresolved image attempt')
    return image_attempt(pid, attempt_id)
