"""Shared N+1 boundary frames for guided character projects."""
import base64
import hashlib
import json
import os
import re
import subprocess
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


def backfill_legacy(db, pid: str) -> bool:
    """Import legacy confirmed first frames as stale start-boundary candidates.

    Runs once: guarded on no active boundaries existing, so repeated calls are
    no-ops. Legacy tables and files are never modified.
    """
    if db.execute('SELECT 1 FROM guided_boundaries WHERE project_id=? AND active=1', (pid,)).fetchone():
        return False
    shots = db.execute('SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,)).fetchall()
    if not shots:
        return False
    ensure_for_storyboard(db, pid)
    for shot in shots:
        selection = db.execute('SELECT confirmed_frame_id FROM guided_frame_selection WHERE project_id=? AND shot_id=?',
                               (pid, shot['id'])).fetchone()
        imported = False
        if selection and selection['confirmed_frame_id']:
            frame_id = selection['confirmed_frame_id']
            version = db.execute('SELECT file FROM guided_frame_versions WHERE id=?', (frame_id,)).fetchone()
            if version and Path(version['file']).is_file():
                detail = db.execute('SELECT prompt, model FROM guided_frame_details WHERE frame_id=?',
                                    (frame_id,)).fetchone()
                refs = [(r['character_id'], r['image_id']) for r in db.execute(
                    'SELECT character_id, image_id FROM guided_frame_characters WHERE frame_id=?', (frame_id,)).fetchall()]
                boundary = db.execute('SELECT id FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                                      (pid, shot['seq'] - 1)).fetchone()
                if boundary:
                    insert_version(db, pid, boundary['id'], Path(version['file']), stale=True,
                                   prompt=detail['prompt'] if detail else '',
                                   model=detail['model'] if detail else '',
                                   references=refs)
                    imported = True
        if imported and db.execute('SELECT 1 FROM timeline_items WHERE project_id=? AND shot_id=?',
                                   (pid, shot['id'])).fetchone():
            db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL',
                       (pid, shot['id']))
    return True


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


_SINGLE_FRAME = (
    'A single cinematic still photograph: one frame, one moment, one camera angle, 16:9 widescreen. '
    'Do not create a storyboard, comic strip, collage, grid, contact sheet, split screen, or multiple panels. '
    'Show one unified scene, not a sequence. The main character appears only once; '
    'do not repeat the character across separate panels. '
    'No text, captions, borders, or watermarks.')


_NOISE = re.compile(
    r'^(?:全片|本镜头|这一镜|镜头|画面|最后[一二三四五六七八九十\d]*秒|特写'
    r'|(?:cut to|camera|close[- ]?up|wide shot|medium shot|long shot|pan|zoom|dissolve|'
    r'fade|tracking|dolly|establishing|the (?:camera|frame|scene))\b)',
    re.IGNORECASE)


def _excerpt(text: str, side: str, limit: int = 160, max_sentences: int = 2) -> str:
    """Keep only the one or two visual beats nearest the cut, not the whole shot."""
    text = ' '.join(text.split())
    parts = [part.strip() for part in
             re.split(r'(?<=[。！？!?])|(?<=\.)\s+', text) if part.strip()]
    parts = [part for part in parts if not _NOISE.match(part)]
    if not parts:
        # Last resort: keep the raw text rather than returning an empty prompt.
        parts = [text]
    chosen = parts[:max_sentences] if side == 'start' else parts[-max_sentences:]
    joined = ''.join(chosen)
    separator = ' ' if re.search(r'[A-Za-z]', joined) else ''
    excerpt = separator.join(chosen).strip()
    return excerpt[:limit].strip() if side == 'start' else excerpt[-limit:].strip()


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
        prompt = f"{_SINGLE_FRAME} Opening frame: {_excerpt(shots[0]['prompt'], 'start')}"
        shot_ids = [shots[0]['id']]
    elif position == len(shots):
        prompt = f"{_SINGLE_FRAME} Closing frame: {_excerpt(shots[-1]['prompt'], 'end')}"
        shot_ids = [shots[-1]['id']]
    else:
        prompt = (f"{_SINGLE_FRAME} The outgoing scene is ending: "
                  f"{_excerpt(shots[position - 1]['prompt'], 'end')} "
                  f"The incoming scene is beginning: {_excerpt(shots[position]['prompt'], 'start')}")
        shot_ids = [shots[position - 1]['id'], shots[position]['id']]
    return {'boundary_id': boundary_id, 'position': position, 'prompt': prompt,
            'references': _references(db, pid, shot_ids)}


_LLM_SYSTEM = (
    'You write one still-frame description for an edit boundary between film shots. '
    'Return JSON {"prompt": "..."} only. Describe a single photographic moment in 2-4 sentences. '
    'Do not include camera movement, cuts, montage, fades, on-screen text, dialogue, storyboards, '
    'panels, or multiple moments. Keep the same subject, clothing, location, lighting and props as '
    'the supplied shots. Write in the same language as the shot descriptions. Output no other keys.')


def _source_payload(db, pid: str, boundary_id: str):
    boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
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
        role, adjacent = 'opening', [shots[0]]
    elif position == len(shots):
        role, adjacent = 'closing', [shots[-1]]
    else:
        role, adjacent = 'transition', [shots[position - 1], shots[position]]
    refs = _references(db, pid, [shot['id'] for shot in adjacent])
    payload = {
        'role': role,
        'position': position,
        'shots': [{'seq': shot['seq'], 'prompt': shot['prompt']} for shot in adjacent],
        'characters': [{'id': ref['id'], 'name': ref['name'], 'description': ref['description'],
                        'image_id': ref['image_id'], 'image_sha256': ref['sha256']}
                       for ref in refs],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return boundary, payload, digest


def _prompt_state(db, pid: str, boundary_id: str) -> dict:
    boundary, payload, digest = _source_payload(db, pid, boundary_id)
    auto = derive(db, pid, boundary_id)
    row = db.execute('SELECT * FROM guided_boundary_prompts WHERE project_id=? AND boundary_id=? '
                     'ORDER BY created DESC, rowid DESC LIMIT 1', (pid, boundary_id)).fetchone()
    current = bool(row and row['source_sha256'] == digest)
    revision = db.execute('SELECT revision FROM guided_projects WHERE project_id=?',
                          (pid,)).fetchone()['revision']
    return {'boundary_id': boundary_id, 'position': auto['position'],
            'auto_prompt': auto['prompt'], 'source_sha256': digest,
            'llm_prompt': row['prompt'] if row else None,
            'llm_prompt_current': current,
            'llm_prompt_model': row['model'] if row else None,
            'llm_prompt_created': row['created'] if row else None,
            'effective_prompt': f'{_SINGLE_FRAME} {row["prompt"]}' if current else auto['prompt'],
            'effective_source': 'llm' if current else 'auto',
            'references': auto['references'], 'revision': revision}


def prompt_state(pid: str, boundary_id: str) -> dict:
    with core.connect() as db:
        return _prompt_state(db, pid, boundary_id)


def preview_prompt(pid: str, boundary_id: str, model: str) -> dict:
    if not isinstance(model, str) or not model.strip():
        raise ValueError('LLM model is required')
    from . import guided_characters, guided_workflow
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        guided_characters.require_confirmed(db, pid)
        row = guided_workflow._row(db, pid)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm storyboard first')
        _, payload, digest = _source_payload(db, pid, boundary_id)
    return {'boundary_id': boundary_id, 'model': model.strip(), 'source_sha256': digest,
            'payload': payload, 'estimated_cost_usd': None,
            'note': 'LLM price is unknown; the budget is not a provider-enforced cap.'}


def _prompt_attempt(pid: str, attempt_id: str) -> dict:
    with core.connect() as db:
        row = db.execute('SELECT * FROM guided_boundary_prompt_attempts WHERE project_id=? AND id=?',
                         (pid, attempt_id)).fetchone()
        if not row:
            raise ValueError('Unknown prompt attempt')
        item = dict(row)
        item['prompt'] = item.get('result_prompt')
        return item


def generate_prompt(pid: str, boundary_id: str, model: str, budget_usd: float, *,
                    confirm_paid: bool, request_id: str, revision: int) -> dict:
    from . import guided_workflow
    details = preview_prompt(pid, boundary_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    aid = uuid4().hex
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        existing = db.execute('SELECT * FROM guided_boundary_prompt_attempts '
                              'WHERE project_id=? AND request_id=?', (pid, request_id)).fetchone()
        if existing:
            if existing['boundary_id'] != boundary_id:
                raise ValueError('Request ID belongs to another boundary')
            item = dict(existing)
            item['prompt'] = item.get('result_prompt')
            return item
        if db.execute("SELECT 1 FROM guided_boundary_prompt_attempts WHERE project_id=? AND boundary_id=? "
                      "AND status IN ('SUBMITTING','UNKNOWN')", (pid, boundary_id)).fetchone():
            raise ValueError('Previous prompt request is unresolved')
        db.execute('INSERT INTO guided_boundary_prompt_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, pid, boundary_id, request_id, 'SUBMITTING', details['model'], budget_usd,
                    details['source_sha256'], None, None, core.now()))
    try:
        result = core._llm_json(_LLM_SYSTEM, json.dumps(details['payload'], ensure_ascii=False),
                                details['model'],
                                os.getenv('STUDIO_LLM_BASE_URL', 'https://api.openai.com/v1'))
        text = result.get('prompt') if isinstance(result, dict) else None
        if not isinstance(text, str) or not 20 <= len(text.strip()) <= 4000:
            raise ValueError('Invalid prompt from the LLM')
        text = text.strip()
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            db.execute('INSERT INTO guided_boundary_prompts VALUES (?,?,?,?,?,?,?)',
                       (uuid4().hex, pid, boundary_id, text, details['source_sha256'],
                        details['model'], core.now()))
            db.execute("UPDATE guided_boundary_prompt_attempts SET status='COMPLETED',result_prompt=? "
                       "WHERE project_id=? AND id=?", (text, pid, aid))
            db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    except Exception as exc:
        with core.connect() as db:
            db.execute("UPDATE guided_boundary_prompt_attempts SET status='UNKNOWN',error=? "
                       "WHERE project_id=? AND id=?", (str(exc)[:500], pid, aid))
    return _prompt_attempt(pid, aid)


def clear_prompt(pid: str, boundary_id: str, revision: int) -> dict:
    from . import guided_workflow
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        if not db.execute('SELECT 1 FROM guided_boundaries WHERE id=? AND project_id=?',
                          (boundary_id, pid)).fetchone():
            raise ValueError('Unknown boundary')
        db.execute('DELETE FROM guided_boundary_prompts WHERE project_id=? AND boundary_id=?',
                   (pid, boundary_id))
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    return prompt_state(pid, boundary_id)


def list_prompt_attempts(pid: str) -> list[dict]:
    with core.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT * FROM guided_boundary_prompt_attempts WHERE project_id=? ORDER BY rowid DESC',
            (pid,))]


def resolve_prompt_attempt(pid: str, attempt_id: str, *, confirm_checked: bool) -> dict:
    if confirm_checked is not True:
        raise ValueError('Confirm that provider charges were checked')
    with core.connect() as db:
        result = db.execute("UPDATE guided_boundary_prompt_attempts SET status='ACKNOWLEDGED' "
                            "WHERE project_id=? AND id=? AND status IN ('SUBMITTING','UNKNOWN')",
                            (pid, attempt_id))
        if result.rowcount != 1:
            raise ValueError('No unresolved prompt attempt')
    return _prompt_attempt(pid, attempt_id)


def ready(db, pid: str, boundary_id: str) -> bool:
    boundary = db.execute('SELECT confirmed_image_id FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
                          (boundary_id, pid)).fetchone()
    return bool(boundary and boundary['confirmed_image_id'])


def insert_version(db, pid: str, boundary_id: str, file: Path, *, stale: bool, prompt: str,
                   model: str, references: list[tuple[str, str]], version_id: str | None = None) -> str:
    path = Path(file).resolve()
    root = (core.HOME / pid).resolve()
    directory = (root / 'guided-images').resolve()
    if not directory.is_relative_to(root) or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError('Image unavailable')
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    version_id = version_id or uuid4().hex
    db.execute('INSERT INTO guided_boundary_versions(id,project_id,boundary_id,file,sha256,stale,created) '
               'VALUES (?,?,?,?,?,?,?)',
               (version_id, pid, boundary_id, str(path), digest.hexdigest(), int(bool(stale)), core.now()))
    db.execute('INSERT INTO guided_boundary_details(frame_id,prompt,model,created) VALUES (?,?,?,?)',
               (version_id, prompt, model, core.now()))
    for character_id, image_id in references:
        db.execute('INSERT INTO guided_boundary_characters(frame_id,character_id,image_id) VALUES (?,?,?)',
                   (version_id, character_id, image_id))
    return version_id


def _adjacent_shot_ids(db, pid: str, position: int) -> list[str]:
    """Active shot ids a boundary position touches (0-based indices p-1 and p)."""
    shots = db.execute('SELECT id FROM shots WHERE project_id=? AND archived=0 ORDER BY seq',
                       (pid,)).fetchall()
    ids = [s['id'] for s in shots]
    return [ids[idx] for idx in (position - 1, position) if 0 <= idx < len(ids)]


def preview(pid: str, boundary_id: str, model: str) -> dict:
    if model != 'gpt-image-2.5-sunburst':
        raise ValueError('Image model capability is unverified')
    from . import guided_images, guided_characters
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        guided_characters.require_confirmed(db, pid)
        settings = db.execute('SELECT storyboard_reconfirmed FROM guided_character_settings WHERE project_id=?',
                              (pid,)).fetchone()
        if not settings or not settings['storyboard_reconfirmed']:
            raise ValueError('Confirm the storyboard for the character workflow')
        state = _prompt_state(db, pid, boundary_id)
        details = {'position': state['position'], 'prompt': state['effective_prompt'],
                   'references': state['references']}
        for shot_id in _adjacent_shot_ids(db, pid, details['position']):
            if not db.execute('SELECT 1 FROM guided_shot_cast_reviewed WHERE project_id=? AND shot_id=?',
                              (pid, shot_id)).fetchone():
                raise ValueError('Review cast for every adjacent shot first')
    for ref in details['references']:
        guided_images.image_path(pid, ref['image_id'])
    return {'boundary_id': boundary_id, 'position': details['position'], 'model': model,
            'prompt': details['prompt'], 'prompt_source': state['effective_source'],
            'llm_prompt_current': state['llm_prompt_current'],
            'references': details['references'],
            'estimated_cost_usd': None, 'note': 'Budget is not a provider-enforced spending cap.'}


def _boundary_snapshot_current(db, pid, details):
    current = _prompt_state(db, pid, details['boundary_id'])
    return (current['effective_prompt'] == details['prompt']
            and current['references'] == details['references'])


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
                                        model=model, references=[(r['id'], r['image_id']) for r in details['references']],
                                        version_id=image_id)
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
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        version = db.execute('SELECT * FROM guided_boundary_versions WHERE project_id=? AND boundary_id=? AND id=?',
                             (pid, boundary_id, image_id)).fetchone()
        if not version:
            raise ValueError('Unknown boundary image')
        if version['stale'] and not allow_stale:
            raise ValueError('This boundary version is stale; crop or regenerate before confirming')
    path = guided_images.image_path(pid, image_id)
    width, height = guided_images.image_dimensions(path)
    if not (256 <= width <= 5760 and 256 <= height <= 5760 and 0.4 <= width / height <= 2.5):
        raise ValueError('Boundary frame dimensions are unsupported')
    if abs(width / height - 16 / 9) > 0.01:
        raise ValueError('Boundary frames must be 16:9; crop the image first')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
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
    source = guided_images.image_path(pid, image_id)
    new_id = uuid4().hex
    target = guided_images._safe_path(pid, new_id, '.png')
    version_id = None
    try:
        guided_images.crop_to_16x9(source, target)
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            version_id = insert_version(db, pid, boundary_id, target, stale=stale, prompt=prompt,
                                        model=model, references=refs, version_id=new_id)
            guided_characters._changed(db, pid)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {'id': version_id, 'revision': revision + 1}


def asset_matches_boundaries(db, pid: str, shot_id: str, asset_id: str) -> bool:
    """True when an asset's recorded video provenance matches the shot's current
    confirmed start/end boundary frames — the single definition of 'current'."""
    shot = db.execute('SELECT seq FROM shots WHERE project_id=? AND id=?', (pid, shot_id)).fetchone()
    if not shot:
        return False
    seq = shot['seq']
    start = db.execute('SELECT confirmed_image_id FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                       (pid, seq - 1)).fetchone()
    end = db.execute('SELECT confirmed_image_id FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                     (pid, seq)).fetchone()
    start_id = start['confirmed_image_id'] if start else None
    end_id = end['confirmed_image_id'] if end else None
    provenance = db.execute('''SELECT p.frame_id, p.last_frame_id FROM guided_attempts a
        JOIN guided_video_provenance p ON p.attempt_id=a.id
        WHERE a.project_id=? AND a.shot_id=? AND a.asset_id=? LIMIT 1''',
        (pid, shot_id, asset_id)).fetchone()
    return bool(provenance and provenance['frame_id'] == start_id
                and provenance['last_frame_id'] == end_id)


def mark_affected(db, pid: str, boundary_id: str) -> list[str]:
    boundary = db.execute('SELECT * FROM guided_boundaries WHERE id=? AND project_id=?',
                          (boundary_id, pid)).fetchone()
    if not boundary or boundary['position'] is None:
        return []
    position = boundary['position']
    shots = db.execute('SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,)).fetchall()
    affected = []
    for idx in (position - 1, position):
        if not 0 <= idx < len(shots):
            continue
        shot = shots[idx]
        selected = db.execute('SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?',
                              (pid, shot['id'])).fetchone()
        if not selected:
            continue
        if not asset_matches_boundaries(db, pid, shot['id'], selected['asset_id']):
            db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL',
                       (pid, shot['id']))
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            affected.append(shot['id'])
    return affected


def shot_ready(db, pid: str, shot_id: str) -> bool:
    start, end = for_shot(db, pid, shot_id)
    return ready(db, pid, start['id']) and ready(db, pid, end['id'])


def _frame_count(path: Path) -> int:
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                                 '-show_entries', 'stream=nb_read_frames', '-of', 'default=nw=1:nk=1',
                                 str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('FFmpeg/FFprobe is required to extract video frames') from exc
    try:
        count = int(result.stdout.strip())
    except ValueError:
        raise ValueError('Could not read video frames') from None
    if result.returncode or count <= 0:
        raise ValueError('Could not read video frames')
    return count


def extract_from_asset(pid: str, seq: int, side: str, revision: int) -> dict:
    """Add the first/last frame of the shot's selected video as an unconfirmed candidate."""
    if side not in ('start', 'end'):
        raise ValueError('side must be start or end')
    from . import guided_images, guided_workflow, timeline, guided_characters
    with core.connect() as db:
        guided_characters._enabled(db, pid)
        row = guided_workflow._row(db, pid)
        guided_workflow._revision(row, revision)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm storyboard first')
        shot = db.execute('SELECT * FROM shots WHERE project_id=? AND seq=? AND archived=0',
                          (pid, seq)).fetchone()
        if not shot:
            raise ValueError('Unknown shot')
        asset = db.execute('''SELECT a.id,a.file,a.sha256 FROM timeline_items t
            JOIN shot_assets a ON a.id=t.asset_id
            WHERE t.project_id=? AND t.shot_id=?''', (pid, shot['id'])).fetchone()
        if not asset:
            raise ValueError('Select a video asset first')
        position = shot['seq'] - 1 if side == 'start' else shot['seq']
        boundary = db.execute('SELECT id FROM guided_boundaries WHERE project_id=? AND active=1 AND position=?',
                              (pid, position)).fetchone()
        if not boundary:
            raise ValueError('Unknown boundary')
    path = Path(asset['file'])
    if not path.is_file() or timeline._hash(path) != asset['sha256']:
        raise ValueError('Selected asset missing or changed')
    core.require_ffmpeg()
    frame = 0 if side == 'start' else _frame_count(path) - 1
    temp = guided_images._safe_path(pid, uuid4().hex + '-extract', '.png')
    final = None
    moved = False
    try:
        vf = (f"select=eq(n\\,{frame}),"
              f"scale=1536:864:force_original_aspect_ratio=increase,crop=1536:864")
        try:
            core.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                      '-vf', vf, '-frames:v', '1', str(temp)])
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('Could not extract a frame; FFmpeg is required') from exc
        if not temp.is_file() or guided_images.image_dimensions(temp) != (1536, 864):
            raise ValueError('Could not extract a valid frame')
        raw = temp.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with core.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            guided_workflow._revision(guided_workflow._row(db, pid), revision)
            if not db.execute('SELECT 1 FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
                              (boundary['id'], pid)).fetchone():
                raise ValueError('Unknown boundary')
            existing = db.execute('SELECT id FROM guided_boundary_versions WHERE project_id=? AND boundary_id=? '
                                  'AND sha256=? LIMIT 1', (pid, boundary['id'], digest)).fetchone()
            if existing:
                return {'id': existing['id'], 'boundary_id': boundary['id'], 'side': side,
                        'revision': revision, 'deduplicated': True}
            image_id = uuid4().hex
            final = guided_images._safe_path(pid, image_id, '.png')
            os.replace(temp, final)
            moved = True
            insert_version(db, pid, boundary['id'], final, stale=False,
                           prompt=f'Extracted {side} frame from asset {asset["id"]}',
                           model='extracted', references=[], version_id=image_id)
            db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
        return {'id': image_id, 'boundary_id': boundary['id'], 'side': side,
                'revision': revision + 1, 'deduplicated': False}
    except Exception:
        if moved and final is not None:
            final.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)


def invalidate_for_shot(db, pid: str, shot_id: str) -> None:
    shot = db.execute('SELECT seq FROM shots WHERE project_id=? AND id=?', (pid, shot_id)).fetchone()
    if not shot:
        return
    boundaries = db.execute('SELECT id FROM guided_boundaries WHERE project_id=? AND active=1 AND position IN (?,?)',
                            (pid, shot['seq'] - 1, shot['seq'])).fetchall()
    for boundary in boundaries:
        db.execute('UPDATE guided_boundary_versions SET stale=1 WHERE project_id=? AND boundary_id=?',
                   (pid, boundary['id']))
        db.execute('UPDATE guided_boundaries SET confirmed_image_id=NULL WHERE id=? AND project_id=?',
                   (boundary['id'], pid))
        mark_affected(db, pid, boundary['id'])


def invalidate_for_character(db, pid: str, character_id: str) -> None:
    boundary_ids = [row['boundary_id'] for row in db.execute(
        'SELECT DISTINCT boundary_id FROM guided_boundary_versions WHERE id IN '
        '(SELECT frame_id FROM guided_boundary_characters WHERE character_id=?)', (character_id,)).fetchall()]
    db.execute('UPDATE guided_boundary_versions SET stale=1 WHERE project_id=? AND id IN '
               '(SELECT frame_id FROM guided_boundary_characters WHERE character_id=?)', (pid, character_id))
    db.execute('UPDATE guided_boundaries SET confirmed_image_id=NULL WHERE project_id=? AND confirmed_image_id IN '
               '(SELECT frame_id FROM guided_boundary_characters WHERE character_id=?)', (pid, character_id))
    for boundary_id in boundary_ids:
        mark_affected(db, pid, boundary_id)
