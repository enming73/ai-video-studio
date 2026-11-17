"""Copy-on-write storyboard revisions, shot versions, and decisions.

Storyboard content truth lives in ``storyboard_revisions``/``shot_revisions``;
``shots`` remains a stable identity row plus a compatibility projection of the
current revision. Every edit clones the current revision (copy-on-write); old
revision rows are immutable. ``decisions`` record human/system
accept/reject/keep_old/confirm/select actions; guided confirmations are
backfilled as human decisions in the legacy materialization path.

``shot_revisions.duration`` is integer seconds (mirrors ``shots.duration``);
persisted timeline times elsewhere use the ``*_us`` (microsecond) suffix.
"""
from __future__ import annotations

import json
from uuid import uuid4

from . import core, edit_model


def _connect(db):
    if db is not None:
        return db, False
    return core.connect(), True


def _ensure(db, pid):
    """Materialize legacy revisions if absent and return the current storyboard
    revision id. Idempotent.

    Uses ``edit_model._current`` (which only materializes when a pointer is
    missing) rather than ``ensure_legacy_revisions`` directly: the latter
    unconditionally rewrites the current pointers back to the legacy revision,
    which would clobber newer copy-on-write revisions.
    """
    state = edit_model._current(db, pid)
    _backfill_decisions(db, pid)
    return state['storyboard_revision_id']


def _backfill_decisions(db, pid):
    """Backfill legacy guided confirmations as human 'confirm' decisions."""
    confirmations = db.execute(
        'SELECT * FROM guided_confirmations WHERE project_id=?', (pid,)).fetchall()
    for row in confirmations:
        exists = db.execute(
            "SELECT 1 FROM decisions WHERE project_id=? AND node_type='shot' AND node_id=? "
            "AND action='confirm' AND json_extract(metadata_json,'$.asset_id')=?",
            (pid, row['shot_id'], row['asset_id'])).fetchone()
        if exists:
            continue
        db.execute(
            'INSERT INTO decisions(id,project_id,node_type,node_id,action,actor,metadata_json,created) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (uuid4().hex, pid, 'shot', row['shot_id'], 'confirm', 'human',
             json.dumps({'asset_id': row['asset_id'], 'prompt': row['prompt']}), core.now()))


def _storyboard(db, pid, revision_id):
    row = db.execute('SELECT * FROM storyboard_revisions WHERE id=? AND project_id=?',
                     (revision_id, pid)).fetchone()
    if row is None:
        raise ValueError('Unknown storyboard revision')
    return row


def _get(db, pid, revision_id, *, ensure=True):
    if ensure:
        _ensure(db, pid)
    sb = _storyboard(db, pid, revision_id)
    scenes = [dict(r) for r in db.execute(
        'SELECT * FROM scenes WHERE storyboard_revision_id=? ORDER BY seq',
        (revision_id,)).fetchall()]
    shots = [dict(r) for r in db.execute(
        'SELECT * FROM shot_revisions WHERE storyboard_revision_id=? ORDER BY seq',
        (revision_id,)).fetchall()]
    decisions = [dict(r) for r in db.execute(
        'SELECT * FROM decisions WHERE project_id=? ORDER BY created, rowid',
        (pid,)).fetchall()]
    return {**dict(sb), 'scenes': scenes, 'shots': shots, 'decisions': decisions}


def get(pid, revision_id, db=None):
    """Return a revision with its scenes, shots, and project decisions."""
    db, own = _connect(db)
    try:
        return _get(db, pid, revision_id)
    finally:
        if own:
            db.commit()


def current(pid, db=None):
    """Return the current storyboard revision, lazily materializing legacy state."""
    db, own = _connect(db)
    try:
        rid = _ensure(db, pid)
        return _get(db, pid, rid, ensure=False)
    finally:
        if own:
            db.commit()


def _next_revision_no(db, pid):
    row = db.execute('SELECT MAX(revision_no) FROM storyboard_revisions WHERE project_id=?',
                     (pid,)).fetchone()
    return (row[0] or 0) + 1


def _clone(db, pid, base_id, *, source, actor, shot_updates):
    """Clone ``base_id`` into a new revision, applying per-shot overrides.

    ``shot_updates`` maps ``shot_id`` to a subset of ``seq``/``prompt``/``duration``.
    The new revision becomes the current storyboard revision pointer.
    """
    base = _storyboard(db, pid, base_id)
    new_id = uuid4().hex
    revision_no = _next_revision_no(db, pid)
    db.execute(
        'INSERT INTO storyboard_revisions(id,project_id,revision_no,source,actor,created) '
        'VALUES (?,?,?,?,?,?)',
        (new_id, pid, revision_no, source, actor, core.now()))

    scene_map = {}
    for scene in db.execute(
            'SELECT * FROM scenes WHERE storyboard_revision_id=? ORDER BY seq',
            (base_id,)).fetchall():
        new_scene_id = uuid4().hex
        scene_map[scene['id']] = new_scene_id
        db.execute(
            'INSERT INTO scenes(id,project_id,storyboard_revision_id,seq,title,description,created) '
            'VALUES (?,?,?,?,?,?,?)',
            (new_scene_id, pid, new_id, scene['seq'], scene['title'], scene['description'], core.now()))

    updates = shot_updates or {}
    for shot in db.execute(
            'SELECT * FROM shot_revisions WHERE storyboard_revision_id=? ORDER BY seq',
            (base_id,)).fetchall():
        up = updates.get(shot['shot_id'], {})
        db.execute(
            'INSERT INTO shot_revisions(id,storyboard_revision_id,shot_id,scene_id,seq,prompt,duration,created) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (uuid4().hex, new_id, shot['shot_id'], scene_map[shot['scene_id']],
             up.get('seq', shot['seq']), up.get('prompt', shot['prompt']),
             up.get('duration', shot['duration']), core.now()))

    db.execute(
        'UPDATE project_state SET current_storyboard_revision_id=?, updated=? WHERE project_id=?',
        (new_id, core.now(), pid))
    return new_id


def _snapshot(db, pid, *, source, actor):
    """Create a new revision from the current ``shots`` compatibility projection.

    Used by the guided structure path, which recomputes ``shots`` (archive,
    insert, reorder, prompt/duration) in one atomic operation before snapshotting.
    """
    revision_no = _next_revision_no(db, pid)
    new_id = uuid4().hex
    db.execute(
        'INSERT INTO storyboard_revisions(id,project_id,revision_no,source,actor,created) '
        'VALUES (?,?,?,?,?,?)',
        (new_id, pid, revision_no, source, actor, core.now()))
    scene_id = uuid4().hex
    db.execute(
        'INSERT INTO scenes(id,project_id,storyboard_revision_id,seq,title,description,created) '
        'VALUES (?,?,?,1,NULL,NULL,?)',
        (scene_id, pid, new_id, core.now()))
    for shot in db.execute(
            'SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq',
            (pid,)).fetchall():
        db.execute(
            'INSERT INTO shot_revisions(id,storyboard_revision_id,shot_id,scene_id,seq,prompt,duration,created) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (uuid4().hex, new_id, shot['id'], scene_id, shot['seq'], shot['prompt'],
             shot['duration'], core.now()))
    db.execute(
        'UPDATE project_state SET current_storyboard_revision_id=?, updated=? WHERE project_id=?',
        (new_id, core.now(), pid))
    return new_id


def _record_decision(db, pid, *, node_type, node_id, action, actor, metadata=None):
    core.project(db, pid)
    decision_id = uuid4().hex
    db.execute(
        'INSERT INTO decisions(id,project_id,node_type,node_id,action,actor,metadata_json,created) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (decision_id, pid, node_type, node_id, action, actor,
         json.dumps(metadata or {}), core.now()))
    return dict(db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone())


def record_decision(pid, *, node_type, node_id, action, actor, metadata=None, db=None):
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        return _record_decision(db, pid, node_type=node_type, node_id=node_id,
                                action=action, actor=actor, metadata=metadata)
    finally:
        if own:
            db.commit()


def _update_shot(db, pid, revision_id, shot_id, *, prompt=None, duration=None,
                 actor='human', source='manual'):
    shot = db.execute(
        'SELECT * FROM shot_revisions WHERE storyboard_revision_id=? AND shot_id=?',
        (revision_id, shot_id)).fetchone()
    if shot is None:
        raise ValueError('Unknown shot')
    new_prompt = shot['prompt']
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('Shot description required')
        new_prompt = prompt.strip()
    new_duration = shot['duration']
    if duration is not None:
        if type(duration) is not int or duration <= 0:
            raise ValueError('Shot duration must be a positive integer')
        new_duration = duration
    updates = {}
    if new_prompt != shot['prompt']:
        updates['prompt'] = new_prompt
    if new_duration != shot['duration']:
        updates['duration'] = new_duration
    new_id = _clone(db, pid, revision_id, source=source, actor=actor,
                    shot_updates={shot_id: updates})
    if updates:
        db.execute('UPDATE shots SET prompt=?, duration=? WHERE id=?',
                   (new_prompt, new_duration, shot_id))
    return _get(db, pid, new_id, ensure=False)


def update_shot(pid, revision_id, shot_id, *, prompt=None, duration=None,
                actor='human', db=None):
    """Copy-on-write: clone the revision and change only ``shot_id``."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        source = 'manual' if actor == 'human' else 'automatic'
        return _update_shot(db, pid, revision_id, shot_id, prompt=prompt,
                            duration=duration, actor=actor, source=source)
    finally:
        if own:
            db.commit()


def reorder(pid, revision_id, ordered_shot_ids, *, actor='human', db=None):
    """Copy-on-write: clone the revision and re-sequence shots to ``ordered_shot_ids``."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        shots = db.execute(
            'SELECT shot_id FROM shot_revisions WHERE storyboard_revision_id=?',
            (revision_id,)).fetchall()
        shot_ids = {s['shot_id'] for s in shots}
        if set(ordered_shot_ids) != shot_ids:
            raise ValueError('ordered_shot_ids must match the revision shots')
        source = 'manual' if actor == 'human' else 'automatic'
        updates = {sid: {'seq': idx} for idx, sid in enumerate(ordered_shot_ids, 1)}
        new_id = _clone(db, pid, revision_id, source=source, actor=actor,
                        shot_updates=updates)
        # Re-sequence the compatibility projection; shift first to avoid the
        # UNIQUE(project_id, seq) collision while reassigning positions.
        db.execute('UPDATE shots SET seq = seq + 1000000 WHERE project_id=? AND archived=0', (pid,))
        for idx, sid in enumerate(ordered_shot_ids, 1):
            db.execute('UPDATE shots SET seq=? WHERE id=?', (idx, sid))
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def create_draft(pid, *, actor='human', source='manual', db=None):
    """Create a draft revision cloning the current one with no changes."""
    db, own = _connect(db)
    try:
        rid = _ensure(db, pid)
        new_id = _clone(db, pid, rid, source=source, actor=actor, shot_updates=None)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def list_revisions(pid, db=None):
    """List storyboard revisions newest-first."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        return [dict(r) for r in db.execute(
            'SELECT * FROM storyboard_revisions WHERE project_id=? ORDER BY revision_no DESC',
            (pid,)).fetchall()]
    finally:
        if own:
            db.commit()
