"""Shared N+1 boundary frames for guided character projects."""
import base64
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from . import core


def ensure_for_storyboard(db, pid: str) -> None:
    if db.execute('SELECT 1 FROM guided_boundaries WHERE project_id=? AND active=1', (pid,)).fetchone():
        return
    count = db.execute('SELECT COUNT(*) FROM shots WHERE project_id=? AND archived=0', (pid,)).fetchone()[0]
    for position in range(count + 1):
        db.execute('INSERT INTO guided_boundaries(id,project_id,position,confirmed_image_id,active,created) '
                   'VALUES (?,?,?,?,?,?)', (uuid4().hex, pid, position, None, 1, core.now()))


def _state(db, boundary):
    candidate_count = db.execute('SELECT COUNT(*) FROM guided_boundary_versions WHERE boundary_id=?',
                                 (boundary['id'],)).fetchone()[0]
    confirmed_stale = False
    if boundary['confirmed_image_id']:
        version = db.execute('SELECT stale FROM guided_boundary_versions WHERE id=?',
                             (boundary['confirmed_image_id'],)).fetchone()
        confirmed_stale = bool(version and version['stale'])
    return {'id': boundary['id'], 'position': boundary['position'],
            'confirmed_image_id': boundary['confirmed_image_id'],
            'confirmed': bool(boundary['confirmed_image_id']),
            'candidate_count': candidate_count, 'confirmed_stale': confirmed_stale}


def list_state(db, pid: str) -> list[dict]:
    rows = db.execute('SELECT * FROM guided_boundaries WHERE project_id=? AND active=1 ORDER BY position',
                      (pid,)).fetchall()
    return [_state(db, b) for b in rows]


def for_shot(db, pid: str, shot_id: str) -> tuple[dict, dict]:
    shot = db.execute('SELECT seq FROM shots WHERE project_id=? AND id=?', (pid, shot_id)).fetchone()
    if not shot:
        raise ValueError('Unknown shot')
    seq = shot['seq']
    start = db.execute('SELECT * FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                       (pid, seq - 1)).fetchone()
    end = db.execute('SELECT * FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                     (pid, seq)).fetchone()
    if not start or not end:
        raise ValueError('Boundary frames not created for this storyboard')
    return _state(db, start), _state(db, end)


def _references(db, pid: str, shot_ids: list[str]) -> list[dict]:
    if not shot_ids:
        return []
    placeholders = ','.join('?' for _ in shot_ids)
    rows = db.execute(f'''SELECT DISTINCT c.id, c.name, c.description,
             c.confirmed_image_id AS image_id, i.sha256
             FROM guided_shot_characters sc
             JOIN guided_characters c ON c.id = sc.character_id
             JOIN guided_character_images i ON i.id = c.confirmed_image_id
             WHERE sc.project_id=? AND sc.shot_id IN ({placeholders})
             ORDER BY c.id''', [pid, *shot_ids]).fetchall()
    return [{'id': r['id'], 'name': r['name'], 'description': r['description'],
             'image_id': r['image_id'], 'sha256': r['sha256']} for r in rows]


def derive(db, pid: str, boundary_id: str) -> dict:
    boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=?',
                          (boundary_id, pid)).fetchone()
    if not boundary:
        raise ValueError('Unknown boundary')
    position = boundary['position']
    if position is None:
        raise ValueError('Archived boundary')
    shots = db.execute('SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq',
                       (pid,)).fetchall()
    if not shots or not 0 <= position <= len(shots):
        raise ValueError('No active shots for this boundary')
    if position == 0:
        prompt = f"Create a cinematic opening frame for shot 1: {shots[0]['prompt']}"
        shot_ids = [shots[0]['id']]
    elif position == len(shots):
        prompt = f"Create a cinematic closing frame for shot {len(shots)}: {shots[-1]['prompt']}"
        shot_ids = [shots[-1]['id']]
    else:
        prompt = (f"Create a cinematic transition frame. End of shot {position}: {shots[position - 1]['prompt']} "
                  f"Next: {shots[position]['prompt']}")
        shot_ids = [shots[position - 1]['id'], shots[position]['id']]
    return {'boundary_id': boundary_id, 'position': position, 'prompt': prompt,
            'references': _references(db, pid, shot_ids)}


def ready(db, pid: str, boundary_id: str) -> bool:
    boundary = db.execute('SELECT confirmed_image_id FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
                          (boundary_id, pid)).fetchone()
    return bool(boundary and boundary['confirmed_image_id'])


def insert_version(db, pid: str, boundary_id: str, file: Path, *, stale: bool, prompt: str,
                   model: str, references: list[tuple[str, str]]) -> str:
    path = Path(file).resolve()
    root = (core.HOME / pid).resolve()
    directory = (root / 'guided-images').resolve()
    if not directory.is_relative_to(root) or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError('Image unavailable')
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    version_id = path.stem
    db.execute('INSERT INTO guided_boundary_versions(id,project_id,boundary_id,file,sha256,stale,created) '
               'VALUES (?,?,?,?,?,?,?)',
               (version_id, pid, boundary_id, str(path), digest.hexdigest(), int(bool(stale)), core.now()))
    db.execute('INSERT INTO guided_boundary_details(frame_id,prompt,model,created) VALUES (?,?,?,?)',
               (version_id, prompt, model, core.now()))
    for character_id, image_id in references:
        db.execute('INSERT INTO guided_boundary_characters(frame_id,character_id,image_id) VALUES (?,?,?)',
                   (version_id, character_id, image_id))
    return version_id


def preview(pid: str, boundary_id: str, model: str) -> dict:
    if model != 'gpt-image-2.5-sunburst':
        raise ValueError('Image model capability is unverified')
    from . import guided_images, guided_characters
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        details = derive(db, pid, boundary_id)
    for ref in details['references']:
        guided_images.image_path(pid, ref['image_id'])
    return {'boundary_id': boundary_id, 'position': details['position'], 'model': model,
            'prompt': details['prompt'], 'references': details['references'],
            'estimated_cost_usd': None, 'note': 'Budget is not a provider-enforced spending cap.'}


def _boundary_snapshot_current(db, pid, details):
    current = derive(db, pid, details['boundary_id'])
    return current['prompt'] == details['prompt'] and current['references'] == details['references']


def generate(pid: str, boundary_id: str, model: str, budget_usd: float, *, confirm_paid: bool,
             request_id: str, revision: int) -> dict:
    from . import guided_images, guided_workflow, guided_characters
    details = preview(pid, boundary_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    load_dotenv()
    key = os.getenv('STUDIO_IMAGE_API_KEY') or os.getenv('OPENAI_API_KEY')
    base = os.getenv('STUDIO_IMAGE_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    if not key or not base.startswith('https://'):
        raise ValueError('Image API key and HTTPS endpoint required')
    references = []
    for ref in details['references']:
        path = guided_images.image_path(pid, ref['image_id'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise ValueError('Reference image changed')
        references.append((ref, path, raw))
    aid = uuid4().hex
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM guided_image_attempts WHERE project_id=? AND request_id=?',
                              (pid, request_id)).fetchone()
        if existing:
            if existing['target_id'] != boundary_id or existing['kind'] != 'boundary':
                raise ValueError('Request ID belongs to another image operation')
            return {k: existing[k] for k in ('id','target_id','kind','request_id','model','budget','status','image_id','error')}
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        if db.execute("SELECT 1 FROM guided_image_attempts WHERE project_id=? AND target_id=? AND kind='boundary' AND status IN ('SUBMITTING','UNKNOWN')",
                      (pid, boundary_id)).fetchone():
            raise ValueError('Previous image submission is unresolved')
        db.execute('INSERT INTO guided_image_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, pid, boundary_id, 'boundary', request_id, model, budget_usd, 'SUBMITTING',
                    json.dumps(details), None, None))
    try:
        if references:
            types = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp'}
            files = [('image[]', (ref['image_id'] + path.suffix, raw, types[path.suffix.lower()])) for ref, path, raw in references]
            data = guided_images._image_edit(base+'/images/edits', key,
                                             {'model':model,'prompt':details['prompt'],'size':'1536x1024','output_format':'png'}, files)
        else:
            data = guided_images._image_request(base+'/images/generations', key,
                                                {'model':model,'prompt':details['prompt'],'size':'1536x1024','output_format':'png'})
        raw = base64.b64decode(data['data'][0]['b64_json'], validate=True)
        if not raw.startswith(guided_images._FORMATS['.png']) or len(raw) > guided_images.MAX_IMAGE_BYTES:
            raise ValueError('Invalid provider image')
        temp = guided_images._safe_path(pid, uuid4().hex + '-raw', '.png')
        with temp.open('xb') as f:
            f.write(raw)
        image_id = uuid4().hex
        target = guided_images._safe_path(pid, image_id, '.png')
        try:
            guided_images.crop_to_16x9(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._row(db, pid)
            stale = int(not _boundary_snapshot_current(db, pid, details))
            version_id = insert_version(db, pid, boundary_id, target, stale=stale, prompt=details['prompt'],
                                        model=model, references=[(r['id'], r['image_id']) for r in details['references']])
            db.execute("UPDATE guided_image_attempts SET status='COMPLETED',image_id=? WHERE project_id=? AND id=?",
                       (version_id, pid, aid))
            guided_characters._changed(db, pid)
    except Exception:
        with core.connect() as db:
            db.execute("UPDATE guided_image_attempts SET status='UNKNOWN',error=? WHERE project_id=? AND id=?",
                       ('Image request failed or outcome unknown; check provider charges.', pid, aid))
    return guided_images.image_attempt(pid, aid)


def list_frames(pid: str, boundary_id: str) -> list[dict]:
    from . import guided_characters
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=?',
                              (boundary_id, pid)).fetchone()
        if not boundary:
            raise ValueError('Unknown boundary')
        rows = db.execute('''SELECT v.id, v.sha256, v.stale, d.prompt, d.model, d.created,
            (b.confirmed_image_id=v.id) AS confirmed FROM guided_boundary_versions v
            LEFT JOIN guided_boundary_details d ON d.frame_id=v.id
            LEFT JOIN guided_boundaries b ON b.id=v.boundary_id AND b.project_id=v.project_id
            WHERE v.project_id=? AND v.boundary_id=? ORDER BY d.created, v.id''', (pid, boundary_id)).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item['references'] = [dict(r) for r in db.execute(
                'SELECT character_id, image_id FROM guided_boundary_characters WHERE frame_id=?', (row['id'],))]
            results.append(item)
        return results


def confirm(pid: str, boundary_id: str, image_id: str, revision: int, *, allow_stale: bool = False) -> dict:
    from . import guided_images, guided_workflow
    path = guided_images.image_path(pid, image_id)
    width, height = guided_images.image_dimensions(path)
    if not (256 <= width <= 5760 and 256 <= height <= 5760 and 0.4 <= width / height <= 2.5):
        raise ValueError('Boundary frame dimensions are unsupported')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        version = db.execute('SELECT * FROM guided_boundary_versions WHERE project_id=? AND boundary_id=? AND id=?',
                             (pid, boundary_id, image_id)).fetchone()
        if not version:
            raise ValueError('Unknown boundary image')
        if version['stale'] and not allow_stale:
            raise ValueError('This boundary version is stale; crop or regenerate before confirming')
        db.execute('UPDATE guided_boundaries SET confirmed_image_id=? WHERE id=? AND project_id=?',
                   (image_id, boundary_id, pid))
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
        affected = mark_affected(db, pid, boundary_id)
    return {'confirmed_image_id': image_id, 'revision': revision + 1, 'affected_shot_ids': affected}


def unconfirm(pid: str, boundary_id: str, revision: int) -> dict:
    from . import guided_workflow
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
                              (boundary_id, pid)).fetchone()
        if not boundary:
            raise ValueError('Unknown boundary')
        db.execute('UPDATE guided_boundaries SET confirmed_image_id=NULL WHERE id=? AND project_id=?',
                   (boundary_id, pid))
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
        affected = mark_affected(db, pid, boundary_id)
    return {'revision': revision + 1, 'affected_shot_ids': affected}


def crop(pid: str, boundary_id: str, image_id: str, revision: int) -> dict:
    from . import guided_images, guided_workflow, guided_characters
    source = guided_images.image_path(pid, image_id)
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        src = db.execute('SELECT * FROM guided_boundary_versions WHERE project_id=? AND boundary_id=? AND id=?',
                         (pid, boundary_id, image_id)).fetchone()
        if not src:
            raise ValueError('Unknown boundary image')
        detail = db.execute('SELECT prompt, model FROM guided_boundary_details WHERE frame_id=?',
                            (image_id,)).fetchone()
        refs = [(r['character_id'], r['image_id']) for r in db.execute(
            'SELECT character_id, image_id FROM guided_boundary_characters WHERE frame_id=?', (image_id,)).fetchall()]
        stale = src['stale']
        prompt = detail['prompt'] if detail else ''
        model = detail['model'] if detail else ''
    new_id = uuid4().hex
    target = guided_images._safe_path(pid, new_id, '.png')
    version_id = None
    try:
        guided_images.crop_to_16x9(source, target)
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            version_id = insert_version(db, pid, boundary_id, target, stale=stale, prompt=prompt,
                                        model=model, references=refs)
            guided_characters._changed(db, pid)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {'id': version_id, 'revision': revision + 1}


def mark_affected(db, pid: str, boundary_id: str) -> list[str]:
    boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=?',
                          (boundary_id, pid)).fetchone()
    if not boundary or boundary['position'] is None:
        return []
    position = boundary['position']
    shots = db.execute('SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,)).fetchall()
    boundaries = db.execute('SELECT * FROM guided_boundaries WHERE project_id=? AND active=1 ORDER BY position',
                            (pid,)).fetchall()
    affected = []
    for idx in (position - 1, position):
        if not 0 <= idx < len(shots):
            continue
        shot = shots[idx]
        selected = db.execute('SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?',
                              (pid, shot['id'])).fetchone()
        if not selected:
            continue
        start = boundaries[idx]['confirmed_image_id'] if idx < len(boundaries) else None
        end = boundaries[idx + 1]['confirmed_image_id'] if idx + 1 < len(boundaries) else None
        provenance = db.execute('''SELECT p.frame_id, p.last_frame_id FROM guided_attempts a
            JOIN guided_video_provenance p ON p.attempt_id=a.id
            WHERE a.project_id=? AND a.shot_id=? AND a.asset_id=? LIMIT 1''',
            (pid, shot['id'], selected['asset_id'])).fetchone()
        if provenance is None or provenance['frame_id'] != start or provenance['last_frame_id'] != end:
            db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL',
                       (pid, shot['id']))
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            affected.append(shot['id'])
    return affected


def shot_ready(db, pid: str, shot_id: str) -> bool:
    start, end = for_shot(db, pid, shot_id)
    return ready(db, pid, start['id']) and ready(db, pid, end['id'])
