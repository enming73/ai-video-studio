"""Persisted, opt-in human approvals for guided production."""
import json
from pathlib import Path
from . import core, timeline, provider_capabilities, guided_storyboard


def create(pid: str) -> dict:
    with core.connect() as db:
        core.project(db, pid)
        if db.execute('SELECT 1 FROM shots WHERE project_id=?', (pid,)).fetchone():
            raise ValueError('Guided mode requires a new project')
        db.execute('INSERT INTO guided_projects(project_id) VALUES (?)', (pid,))
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
        shots = db.execute('''SELECT s.id, s.seq, s.prompt, s.duration, t.asset_id,
             c.asset_id AS confirmed_asset, c.prompt AS confirmed_prompt
             FROM shots s LEFT JOIN timeline_items t ON t.project_id=s.project_id AND t.shot_id=s.id
             LEFT JOIN guided_confirmations c ON c.project_id=s.project_id AND c.shot_id=s.id
             WHERE s.project_id=? ORDER BY s.seq''', (pid,)).fetchall()
        return {'project_id': pid, 'revision': row['revision'],
                'draft': json.loads(row['draft_json']) if row['draft_json'] else None,
                'storyboard_confirmed': bool(row['storyboard_confirmed']),
                'shots': [{**dict(s), 'confirmed': bool(s['asset_id'] and s['asset_id'] == s['confirmed_asset']
                                                        and s['prompt'] == s['confirmed_prompt'])} for s in shots]}


def save_draft(pid: str, draft: dict, revision: int) -> dict:
    if not isinstance(draft, dict) or not isinstance(draft.get('shots'), list) or not isinstance(draft.get('character_bible'), dict):
        raise ValueError('Invalid draft')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, pid)
        _revision(row, revision)
        if row['storyboard_confirmed']:
            raise ValueError('Storyboard already confirmed')
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
        db.execute('INSERT INTO storyboards VALUES (?,?,?,?)', (pid, json.dumps(draft['character_bible']), 'guided', core.now()))
        for seq, shot in enumerate(shots, 1):
            db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)',
                       (f'{pid}-s{seq:03d}', pid, seq, shot['prompt'].strip(), shot['duration'], 'PLANNED'))
        db.execute("UPDATE projects SET status='PLANNED' WHERE id=?", (pid,))
        db.execute('UPDATE guided_projects SET storyboard_confirmed=1,revision=revision+1 WHERE project_id=?', (pid,))
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
        missing = db.execute('''SELECT COUNT(*) FROM shots s LEFT JOIN guided_confirmations c
            ON c.project_id=s.project_id AND c.shot_id=s.id WHERE s.project_id=? AND s.seq<? AND c.shot_id IS NULL''', (pid, seq)).fetchone()[0]
        if missing:
            raise ValueError('Confirm earlier shots first')
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
            db.execute('UPDATE shots SET prompt=? WHERE id=?', (prompt.strip(), shot['id']))
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, shot['id']))
            db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    return get(pid)


def render_ready(pid: str) -> bool:
    state = get(pid)
    return bool(state['storyboard_confirmed'] and state['shots'] and all(s['confirmed'] for s in state['shots']))
