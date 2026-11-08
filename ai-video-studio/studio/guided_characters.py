"""Versioned, explicitly approved characters for opt-in guided projects."""
import json
from pathlib import Path
from uuid import uuid4
from . import core, guided_workflow, timeline


def _enabled(db, pid):
    guided_workflow._row(db, pid)
    row = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
    if not row:
        raise ValueError('Character stage is not enabled')
    return row


def _validate_ids(db, pid, ids):
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('Explicit, unique character IDs required for each shot')
    for cid in ids:
        if not db.execute('SELECT 1 FROM guided_characters WHERE id=? AND project_id=?', (cid, pid)).fetchone():
            raise ValueError('Unknown character')


def _changed(db, pid):
    db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))


def activate(pid: str, revision: int) -> dict:
    with core.connect() as db:
        timeline.migrate(db)
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Character stage already enabled')
        db.execute('INSERT INTO guided_character_settings(project_id) VALUES (?)', (pid,))
        db.execute('''INSERT OR IGNORE INTO guided_stale_shots(project_id,shot_id,keep_asset_id)
                      SELECT project_id,shot_id,NULL FROM timeline_items WHERE project_id=?''', (pid,))
        _changed(db, pid)
    return get(pid)


def get(pid: str) -> dict:
    with core.connect() as db:
        row = guided_workflow._row(db, pid)
        settings = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
        roles = db.execute('SELECT * FROM guided_characters WHERE project_id=? ORDER BY rowid', (pid,)).fetchall()
        return {'project_id': pid, 'revision': row['revision'], 'enabled': bool(settings),
                'storyboard_reconfirmed': bool(settings and settings['storyboard_reconfirmed']),
                'empty_cast_confirmed': bool(settings and settings['empty_cast_confirmed']),
                'characters': [{**dict(r), 'confirmed': bool(r['confirmed_image_id'] and
                    r['confirmed_image_id'] == r['selected_image_id'] and r['confirmed_description'] == r['description'])}
                    for r in roles]}


def _sync_bible(db, pid):
    if not db.execute('SELECT 1 FROM storyboards WHERE project_id=?', (pid,)).fetchone():
        return
    bible = {r['name']: r['description'] for r in db.execute(
        'SELECT name,description FROM guided_characters WHERE project_id=? ORDER BY rowid', (pid,))}
    db.execute('UPDATE storyboards SET bible_json=? WHERE project_id=?',
               (json.dumps(bible, ensure_ascii=False), pid))


def save_character(pid: str, character_id: str | None, name: str, description: str, revision: int) -> dict:
    if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip():
        raise ValueError('Character name and description required')
    name, description = name.strip(), description.strip()
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        if db.execute('SELECT 1 FROM guided_characters WHERE project_id=? AND name=? COLLATE NOCASE AND id!=?',
                      (pid, name, character_id or '')).fetchone():
            raise ValueError('Duplicate character name')
        if character_id:
            old = db.execute('SELECT * FROM guided_characters WHERE id=? AND project_id=?', (character_id, pid)).fetchone()
            if not old: raise ValueError('Unknown character')
            if old['name'] != name or old['description'] != description:
                _invalidate_character(db, pid, character_id)
                db.execute('UPDATE guided_characters SET name=?,description=?,confirmed_image_id=NULL,confirmed_description=NULL WHERE id=?',
                           (name, description, character_id))
        else:
            character_id = uuid4().hex
            db.execute('INSERT INTO guided_characters(id,project_id,name,description) VALUES (?,?,?,?)',
                       (character_id, pid, name, description))
            db.execute('UPDATE guided_character_settings SET empty_cast_confirmed=0 WHERE project_id=?', (pid,))
        db.execute('INSERT INTO guided_character_history VALUES (?,?,?,?,?,?)',
                   (uuid4().hex, pid, character_id, name, description, core.now()))
        _sync_bible(db, pid)
        _changed(db, pid)
    return get(pid)


def _invalidate_character(db, pid, cid):
    db.execute('UPDATE guided_frame_versions SET stale=1 WHERE project_id=? AND id IN (SELECT frame_id FROM guided_frame_characters WHERE character_id=?)', (pid, cid))
    db.execute('DELETE FROM guided_frame_selection WHERE project_id=? AND confirmed_frame_id IN (SELECT frame_id FROM guided_frame_characters WHERE character_id=?)', (pid, cid))
    for row in db.execute('SELECT shot_id FROM guided_shot_characters WHERE project_id=? AND character_id=?', (pid, cid)):
        db.execute('INSERT INTO guided_stale_shots(project_id,shot_id,keep_asset_id) VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL', (pid, row['shot_id']))
    from . import guided_boundaries
    guided_boundaries.invalidate_for_character(db, pid, cid)


def invalidate_shot(db, pid, shot_id):
    if not db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone(): return
    db.execute('UPDATE guided_frame_versions SET stale=1 WHERE project_id=? AND shot_id=?', (pid, shot_id))
    db.execute('DELETE FROM guided_frame_selection WHERE project_id=? AND shot_id=?', (pid, shot_id))
    db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL', (pid, shot_id))
    from . import guided_boundaries
    guided_boundaries.invalidate_for_shot(db, pid, shot_id)


def confirm_character(pid: str, character_id: str, image_id: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        role = db.execute('SELECT * FROM guided_characters WHERE id=? AND project_id=?', (character_id, pid)).fetchone()
        image = db.execute('SELECT * FROM guided_character_images WHERE id=? AND character_id=? AND project_id=?', (image_id, character_id, pid)).fetchone()
        if not role or not image or not Path(image['file']).is_file() or timeline._hash(Path(image['file'])) != image['sha256']:
            raise ValueError('Character image unavailable or changed')
        if role['selected_image_id'] != image_id:
            _invalidate_character(db, pid, character_id)
        db.execute('UPDATE guided_characters SET selected_image_id=?,confirmed_image_id=?,confirmed_description=description WHERE id=?', (image_id, image_id, character_id))
        _changed(db, pid)
    return get(pid)


def select_character_image(pid: str, character_id: str, image_id: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        role = db.execute('SELECT * FROM guided_characters WHERE id=? AND project_id=?', (character_id, pid)).fetchone()
        image = db.execute('SELECT * FROM guided_character_images WHERE id=? AND character_id=? AND project_id=?', (image_id, character_id, pid)).fetchone()
        if not role or not image or not Path(image['file']).is_file() or timeline._hash(Path(image['file'])) != image['sha256']:
            raise ValueError('Character image unavailable or changed')
        if role['selected_image_id'] != image_id:
            _invalidate_character(db, pid, character_id)
            db.execute('UPDATE guided_characters SET selected_image_id=?,confirmed_image_id=NULL,confirmed_description=NULL WHERE id=?', (image_id, character_id))
            _changed(db, pid)
    return get(pid)


def confirm_empty_cast(pid: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        if db.execute('SELECT 1 FROM guided_characters WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Project contains characters')
        db.execute('UPDATE guided_character_settings SET empty_cast_confirmed=1 WHERE project_id=?', (pid,))
        _changed(db, pid)
    return get(pid)


def import_existing_bible(pid: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = guided_workflow._row(db, pid)
        guided_workflow._revision(row, revision)
        _enabled(db, pid)
        if db.execute('SELECT 1 FROM guided_characters WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Characters already exist; add or edit them manually')
        draft = json.loads(row['draft_json']) if row['draft_json'] else {}
        suggested = draft.get('characters')
        if not isinstance(suggested, list):
            board = db.execute('SELECT bible_json FROM storyboards WHERE project_id=?', (pid,)).fetchone()
            bible = json.loads(board['bible_json']) if board else draft.get('character_bible')
            if not isinstance(bible, dict) or not bible:
                raise ValueError('No existing character suggestions')
            suggested = [{'id': None, 'name': name, 'description': description}
                         for name, description in bible.items()]
        for role in suggested:
            if not isinstance(role, dict):
                raise ValueError('Invalid existing character')
            name, description = role.get('name'), role.get('description')
            if isinstance(description, dict):
                description = json.dumps(description, ensure_ascii=False)
            if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip():
                raise ValueError('Invalid existing character')
            cid = role.get('id') or uuid4().hex
            if db.execute('SELECT 1 FROM guided_characters WHERE id=?', (cid,)).fetchone():
                raise ValueError('Character ID already in use')
            db.execute('INSERT INTO guided_characters(id,project_id,name,description) VALUES (?,?,?,?)',
                       (cid, pid, name.strip(), description.strip()))
            db.execute('INSERT INTO guided_character_history VALUES (?,?,?,?,?,?)',
                       (uuid4().hex, pid, cid, name.strip(), description.strip(), core.now()))
        db.execute('UPDATE guided_character_settings SET empty_cast_confirmed=0 WHERE project_id=?', (pid,))
        _sync_bible(db, pid)
        _changed(db, pid)
    return get(pid)


def require_confirmed(db, pid):
    settings = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
    if not settings: return
    roles = db.execute('SELECT * FROM guided_characters WHERE project_id=?', (pid,)).fetchall()
    if not roles and not settings['empty_cast_confirmed']:
        raise ValueError('Confirm characters or explicitly confirm an empty cast')
    if any(not r['confirmed_image_id'] or r['confirmed_image_id'] != r['selected_image_id'] or r['description'] != r['confirmed_description'] for r in roles):
        raise ValueError('Confirm all characters first')


def set_shot_characters(pid: str, seq: int, character_ids: list[str], revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        _validate_ids(db, pid, character_ids)
        shot = db.execute('SELECT id FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
        if not shot: raise ValueError('Unknown shot')
        previous = {r['character_id'] for r in db.execute('SELECT character_id FROM guided_shot_characters WHERE project_id=? AND shot_id=?', (pid, shot['id']))}
        reviewed = db.execute('SELECT 1 FROM guided_shot_cast_reviewed WHERE project_id=? AND shot_id=?', (pid, shot['id'])).fetchone()
        if previous != set(character_ids):
            from . import guided_boundaries
            guided_boundaries.invalidate_for_shot(db, pid, shot['id'])
            db.execute('UPDATE guided_frame_versions SET stale=1 WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            db.execute('DELETE FROM guided_frame_selection WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL', (pid, shot['id']))
            db.execute('DELETE FROM guided_shot_characters WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            for cid in character_ids:
                db.execute('INSERT INTO guided_shot_characters VALUES (?,?,?)', (pid, shot['id'], cid))
        if previous != set(character_ids) or not reviewed:
            db.execute('INSERT OR IGNORE INTO guided_shot_cast_reviewed VALUES (?,?)', (pid, shot['id']))
            _changed(db, pid)
    return get(pid)


def reconfirm_storyboard(pid: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = guided_workflow._row(db, pid)
        guided_workflow._revision(row, revision)
        _enabled(db, pid)
        require_confirmed(db, pid)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm the draft storyboard first')
        missing = db.execute('''SELECT 1 FROM shots s LEFT JOIN guided_shot_cast_reviewed r
            ON r.project_id=s.project_id AND r.shot_id=s.id
            WHERE s.project_id=? AND s.archived=0 AND r.shot_id IS NULL LIMIT 1''', (pid,)).fetchone()
        if missing: raise ValueError('Review cast for every shot first')
        db.execute('UPDATE guided_character_settings SET storyboard_reconfirmed=1 WHERE project_id=?', (pid,))
        _changed(db, pid)
    return get(pid)


def keep_old_video(pid: str, seq: int, asset_id: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        _enabled(db, pid)
        shot = db.execute('SELECT id FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
        if not shot: raise ValueError('Unknown shot')
        selected = db.execute('SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?', (pid, shot['id'])).fetchone()
        stale = db.execute('SELECT 1 FROM guided_stale_shots WHERE project_id=? AND shot_id=?', (pid, shot['id'])).fetchone()
        if not stale or not selected or selected['asset_id'] != asset_id:
            raise ValueError('Select the affected old video first')
        db.execute('UPDATE guided_stale_shots SET keep_asset_id=? WHERE project_id=? AND shot_id=?', (asset_id, pid, shot['id']))
        _changed(db, pid)
    return get(pid)


def require_render_ready(db, pid):
    settings = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
    if not settings: return
    require_confirmed(db, pid)
    if not settings['storyboard_reconfirmed']:
        raise ValueError('Confirm the storyboard for the character workflow')
    unresolved = db.execute('''SELECT 1 FROM guided_stale_shots s JOIN timeline_items t
        ON t.project_id=s.project_id AND t.shot_id=s.shot_id
        WHERE s.project_id=? AND (s.keep_asset_id IS NULL OR s.keep_asset_id!=t.asset_id) LIMIT 1''', (pid,)).fetchone()
    if unresolved: raise ValueError('Decide whether to keep each affected old video')
