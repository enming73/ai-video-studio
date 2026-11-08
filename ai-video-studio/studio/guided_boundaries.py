"""Shared N+1 boundary frames for guided character projects."""
from uuid import uuid4
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
