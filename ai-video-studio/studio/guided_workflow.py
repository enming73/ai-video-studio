"""Persisted, opt-in human approvals for guided production."""
import json
from pathlib import Path
from . import core, timeline, provider_capabilities, guided_storyboard


def create(pid: str, *, character_stage_enabled: bool = False) -> dict:
    with core.connect() as db:
        core.project(db, pid)
        if db.execute('SELECT 1 FROM shots WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Guided mode requires a new project')
        db.execute('INSERT INTO guided_projects(project_id) VALUES (?)', (pid,))
        if character_stage_enabled:
            db.execute('INSERT INTO guided_character_settings(project_id) VALUES (?)', (pid,))
    return get(pid)


def _row(db, pid):
    core.project(db, pid)
    row = db.execute('SELECT * FROM guided_projects WHERE project_id=?', (pid,)).fetchone()
    if row is None:
        raise ValueError('Not a guided project')
    return row


def _revision(row, revision):
    if type(revision) is not int or row['revision'] != revision:
        raise ValueError('Stale workflow revision; reload')


def get(pid: str) -> dict:
    with core.connect() as db:
        timeline.migrate(db)
        row = _row(db, pid)
        settings = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
        boundary_state = None
        boundary_by_position = {}
        if settings:
            from . import guided_boundaries
            boundary_state = guided_boundaries.list_state(db, pid)
            boundary_by_position = {b['position']: b for b in boundary_state}
        shots = db.execute('''SELECT s.id, s.seq, s.prompt, s.duration, t.asset_id,
             c.asset_id AS confirmed_asset, c.prompt AS confirmed_prompt
             FROM shots s LEFT JOIN timeline_items t ON t.project_id=s.project_id AND t.shot_id=s.id
             LEFT JOIN guided_confirmations c ON c.project_id=s.project_id AND c.shot_id=s.id
             WHERE s.project_id=? ORDER BY s.seq''', (pid,)).fetchall()
        shot_data = []
        for s in shots:
            item = {**dict(s), 'confirmed': bool(s['asset_id'] and s['asset_id'] == s['confirmed_asset']
                                                   and s['prompt'] == s['confirmed_prompt'])}
            if settings:
                item['character_ids'] = [r['character_id'] for r in db.execute(
                    'SELECT character_id FROM guided_shot_characters WHERE project_id=? AND shot_id=? ORDER BY character_id', (pid, s['id']))]
                item['cast_reviewed'] = bool(db.execute('SELECT 1 FROM guided_shot_cast_reviewed WHERE project_id=? AND shot_id=?', (pid,s['id'])).fetchone())
                frame = db.execute('SELECT confirmed_frame_id FROM guided_frame_selection WHERE project_id=? AND shot_id=?', (pid,s['id'])).fetchone()
                stale = db.execute('SELECT keep_asset_id FROM guided_stale_shots WHERE project_id=? AND shot_id=?', (pid,s['id'])).fetchone()
                provenance = db.execute('''SELECT p.frame_id,p.frame_sha256,p.model FROM guided_attempts a
                    JOIN guided_video_provenance p ON p.attempt_id=a.id
                    WHERE a.project_id=? AND a.shot_id=? AND a.asset_id=? LIMIT 1''', (pid,s['id'],s['asset_id'])).fetchone()
                item['confirmed_frame_id'] = frame['confirmed_frame_id'] if frame else None
                item['old_video_decision_required'] = bool(stale and s['asset_id'] and stale['keep_asset_id'] != s['asset_id'])
                item['video_provenance'] = dict(provenance) if provenance else None
                item['start_boundary_id'] = (boundary_by_position.get(s['seq'] - 1) or {}).get('id')
                item['end_boundary_id'] = (boundary_by_position.get(s['seq']) or {}).get('id')
            shot_data.append(item)
        choice = db.execute('SELECT provider,model FROM guided_video_choices WHERE project_id=?', (pid,)).fetchone()
        return {'project_id': pid, 'revision': row['revision'], 'character_stage_enabled': bool(settings),
                'storyboard_reconfirmed': bool(settings and settings['storyboard_reconfirmed']),
                'video_model': dict(choice) if choice else None,
                'draft': json.loads(row['draft_json']) if row['draft_json'] else None,
                'storyboard_confirmed': bool(row['storyboard_confirmed']),
                'shots': shot_data, 'boundaries': boundary_state}


def save_draft(pid: str, draft: dict, revision: int) -> dict:
    if not isinstance(draft, dict) or not isinstance(draft.get('shots'), list) or not isinstance(draft.get('character_bible'), dict):
        raise ValueError('Invalid draft')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, pid)
        _revision(row, revision)
        if row['storyboard_confirmed']:
            raise ValueError('Storyboard already confirmed')
        if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
            roles = draft.get('characters', [])
            if not isinstance(roles, list): raise ValueError('Invalid suggested characters')
            for role in roles:
                if (not isinstance(role, dict) or not isinstance(role.get('id'), str) or not role['id']
                    or not isinstance(role.get('name'), str) or not role['name'].strip()
                    or not isinstance(role.get('description'), str) or not role['description'].strip()):
                    raise ValueError('Invalid suggested character')
                existing = db.execute('SELECT project_id FROM guided_characters WHERE id=?', (role['id'],)).fetchone()
                if existing and existing['project_id'] != pid: raise ValueError('Character belongs to another project')
                if not existing:
                    duplicate = db.execute('SELECT 1 FROM guided_characters WHERE project_id=? AND name=? COLLATE NOCASE', (pid, role['name'].strip())).fetchone()
                    if duplicate: raise ValueError('Suggested character name already exists')
                    db.execute('INSERT INTO guided_characters(id,project_id,name,description) VALUES (?,?,?,?)',
                               (role['id'], pid, role['name'].strip(), role['description'].strip()))
        db.execute('UPDATE guided_projects SET draft_json=?,revision=revision+1 WHERE project_id=?',
                   (json.dumps(draft, ensure_ascii=False), pid))
    return get(pid)


def confirm_storyboard(pid: str, revision: int) -> dict:
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, pid)
        _revision(row, revision)
        if row['storyboard_confirmed'] or not row['draft_json']:
            raise ValueError('No unconfirmed draft')
        from . import guided_characters
        guided_characters.require_confirmed(db, pid)
        if db.execute('SELECT 1 FROM shots WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Existing shots cannot be replaced')
        draft = json.loads(row['draft_json'])
        shots = draft['shots']
        if any(not isinstance(s, dict) or not isinstance(s.get('prompt'), str) or not s['prompt'].strip() for s in shots):
            raise ValueError('Shot description required')
        p = core.project(db, pid)
        cap = provider_capabilities.capabilities(p['provider'], guided_storyboard._MODELS[p['provider']])
        if not cap['verified']:
            raise ValueError('Unverified provider')
        provider_capabilities.validate_durations(p['duration'], [s.get('duration') for s in shots], cap['durations'])
        bible = draft['character_bible']
        if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
            bible = {r['name']:r['description'] for r in db.execute('SELECT name,description FROM guided_characters WHERE project_id=?', (pid,))}
        db.execute('INSERT INTO storyboards VALUES (?,?,?,?)', (pid, json.dumps(bible, ensure_ascii=False), 'guided', core.now()))
        for seq, shot in enumerate(shots, 1):
            db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)',
                       (f'{pid}-s{seq:03d}', pid, seq, shot['prompt'].strip(), shot['duration'], 'PLANNED'))
            if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
                ids = shot.get('character_ids')
                guided_characters._validate_ids(db, pid, ids)
                for cid in ids:
                    db.execute('INSERT INTO guided_shot_characters VALUES (?,?,?)', (pid, f'{pid}-s{seq:03d}', cid))
                db.execute('INSERT INTO guided_shot_cast_reviewed VALUES (?,?)', (pid, f'{pid}-s{seq:03d}'))
        db.execute("UPDATE projects SET status='PLANNED' WHERE id=?", (pid,))
        db.execute('UPDATE guided_projects SET storyboard_confirmed=1,revision=revision+1 WHERE project_id=?', (pid,))
        db.execute('UPDATE guided_character_settings SET storyboard_reconfirmed=1 WHERE project_id=?', (pid,))
        if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
            from . import guided_boundaries
            guided_boundaries.ensure_for_storyboard(db, pid)
    return get(pid)


def confirm_shot(pid: str, seq: int, asset_id: str, revision: int) -> dict:
    with core.connect() as db:
        timeline.migrate(db)
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, pid)
        _revision(row, revision)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm storyboard first')
        shot = db.execute('SELECT * FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
        if not shot:
            raise ValueError('Unknown shot')
        asset = db.execute('''SELECT a.file,a.sha256 FROM timeline_items t JOIN shot_assets a ON a.id=t.asset_id
            WHERE t.project_id=? AND t.shot_id=? AND t.asset_id=?''', (pid,shot['id'],asset_id)).fetchone()
        if not asset or not Path(asset['file']).is_file() or timeline._hash(Path(asset['file'])) != asset['sha256']:
            raise ValueError('Selected asset missing or changed')
        db.execute('INSERT OR REPLACE INTO guided_confirmations VALUES (?,?,?,?)', (pid, shot['id'], asset_id, shot['prompt']))
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    return get(pid)


def edit_shot(pid: str, seq: int, prompt: str, revision: int) -> dict:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Shot description required')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, pid)
        _revision(row, revision)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm storyboard first')
        shot = db.execute('SELECT * FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
        if not shot:
            raise ValueError('Unknown shot')
        if prompt.strip() != shot['prompt']:
            from . import guided_characters, guided_boundaries
            guided_characters.invalidate_shot(db, pid, shot['id'])
            guided_boundaries.invalidate_for_shot(db, pid, shot['id'])
            db.execute('UPDATE shots SET prompt=? WHERE id=?', (prompt.strip(), shot['id']))
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    return get(pid)


def render_ready(pid: str) -> bool:
    state = get(pid)
    if not (state['storyboard_confirmed'] and state['shots'] and all(s['confirmed'] for s in state['shots'])):
        return False
    from . import guided_characters
    with core.connect() as db:
        try: guided_characters.require_render_ready(db, pid)
        except ValueError: return False
    return True
