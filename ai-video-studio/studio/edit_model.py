"""M2 minimal edit model: revisioned storyboard/timeline and lazy legacy
materialization.

Revision identifiers are the only truth for content; ``shots.id`` stays stable
across revisions. Persisted revision times use integer microseconds and carry
the ``_us`` suffix. ``timeline_items`` is a compatibility projection of the
current timeline revision, and ``sync_legacy_projection`` is its only new
writer.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

from . import core

MICRO = 1_000_000


def _now() -> str:
    return core.now()


def _connect(db):
    """Open a connection when the caller did not supply one.

    ``core.connect()`` also runs migrations, so callers that pass a raw
    ``sqlite3`` connection from ``core.connect()`` already have the M2 tables.
    """
    if db is not None:
        return db, False
    return core.connect(), True


def _ensure_state(db, pid: str) -> dict:
    core.project(db, pid)
    row = db.execute('SELECT * FROM project_state WHERE project_id=?', (pid,)).fetchone()
    if row is None:
        db.execute('INSERT INTO project_state(project_id, updated) VALUES (?,?)',
                   (pid, _now()))
        row = db.execute('SELECT * FROM project_state WHERE project_id=?', (pid,)).fetchone()
    return dict(row)


def ensure_state(pid: str, db=None) -> dict:
    """Create the ``project_state`` row for ``pid`` if absent and return it."""
    db, own = _connect(db)
    try:
        return _ensure_state(db, pid)
    finally:
        if own:
            db.commit()


def _hash_file(path) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.is_file() or p.stat().st_size == 0:
            return ''
        h = hashlib.sha256()
        with p.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ''


def _probe_duration_us(path) -> int | None:
    """Best-effort media duration probe; None when FFprobe is unavailable or
    the file is not probeable (e.g. fake test bytes)."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, check=True)
        seconds = float(result.stdout.strip())
        return int(round(seconds * MICRO))
    except Exception:
        return None


def _json_file(pid: str, name: str):
    path = core.HOME / pid / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _ensure_legacy_revisions(db, pid: str) -> dict:
    core.project(db, pid)
    from . import timeline
    timeline.migrate(db)

    sb = db.execute(
        'SELECT * FROM storyboard_revisions WHERE project_id=? AND revision_no=1',
        (pid,)).fetchone()
    if sb is None:
        sb_id = uuid4().hex
        db.execute(
            'INSERT INTO storyboard_revisions(id,project_id,revision_no,source,actor,created) '
            "VALUES (?,?,1,'legacy','legacy',?)",
            (sb_id, pid, _now()))
    else:
        sb_id = sb['id']

    scene = db.execute(
        'SELECT * FROM scenes WHERE storyboard_revision_id=? AND seq=1', (sb_id,)).fetchone()
    if scene is None:
        scene_id = uuid4().hex
        db.execute(
            'INSERT INTO scenes(id,project_id,storyboard_revision_id,seq,title,description,created) '
            'VALUES (?,?,?,1,NULL,NULL,?)',
            (scene_id, pid, sb_id, _now()))
    else:
        scene_id = scene['id']

    shots = db.execute(
        'SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,)).fetchall()
    for shot in shots:
        exists = db.execute(
            'SELECT 1 FROM shot_revisions WHERE storyboard_revision_id=? AND shot_id=?',
            (sb_id, shot['id'])).fetchone()
        if exists:
            continue
        db.execute(
            'INSERT INTO shot_revisions(id,storyboard_revision_id,shot_id,scene_id,seq,prompt,duration,created) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (uuid4().hex, sb_id, shot['id'], scene_id, shot['seq'],
             shot['prompt'], shot['duration'], _now()))

    tl = db.execute(
        'SELECT * FROM timeline_revisions WHERE project_id=? AND revision_no=1',
        (pid,)).fetchone()
    if tl is None:
        tl_id = uuid4().hex
        duration_us = (sum(s['duration'] for s in shots)
                       if shots else int(core.project(db, pid)['duration'])) * MICRO
        db.execute(
            'INSERT INTO timeline_revisions(id,project_id,storyboard_revision_id,revision_no,fps,duration_us,source,actor,created) '
            "VALUES (?,?,?,1,24,?,'legacy','legacy',?)",
            (tl_id, pid, sb_id, duration_us, _now()))
    else:
        tl_id = tl['id']

    _materialize_video_clips(db, pid, tl_id, shots)
    _materialize_audio_clips(db, pid, tl_id)
    _materialize_subtitles(db, pid, tl_id)

    db.execute(
        'INSERT INTO project_state(project_id,current_storyboard_revision_id,current_timeline_revision_id,updated) '
        'VALUES (?,?,?,?) '
        'ON CONFLICT(project_id) DO UPDATE SET '
        'current_storyboard_revision_id=excluded.current_storyboard_revision_id, '
        'current_timeline_revision_id=excluded.current_timeline_revision_id, '
        'updated=excluded.updated',
        (pid, sb_id, tl_id, _now()))

    return {'storyboard_revision_id': sb_id, 'timeline_revision_id': tl_id}


def _materialize_video_clips(db, pid: str, tl_id: str, shots) -> None:
    """Derive video clips from ``timeline_items``; one clip per selected shot.

    ``source_in_us`` is 0; ``source_out_us`` is the probeable asset duration or
    the shot duration fallback. Clips tile contiguously on the timeline.
    """
    offset_us = 0
    for shot in shots:
        item = db.execute(
            'SELECT * FROM timeline_items WHERE project_id=? AND shot_id=?',
            (pid, shot['id'])).fetchone()
        if item is None:
            continue
        asset = db.execute(
            'SELECT * FROM shot_assets WHERE id=?', (item['asset_id'],)).fetchone()
        if asset is None:
            continue
        existing = db.execute(
            'SELECT 1 FROM timeline_video_clips WHERE timeline_revision_id=? AND shot_id=?',
            (tl_id, shot['id'])).fetchone()
        if existing:
            continue
        source_out_us = _probe_duration_us(asset['file']) or (shot['duration'] * MICRO)
        db.execute(
            'INSERT INTO timeline_video_clips(id,timeline_revision_id,shot_id,asset_id,position,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, tl_id, shot['id'], asset['id'], shot['seq'],
             0, source_out_us, offset_us, offset_us + source_out_us, _now()))
        offset_us += source_out_us


def _materialize_audio_clips(db, pid: str, tl_id: str) -> None:
    """Import sound config (narration/music/ambience) and ``audio-events.json``
    (sfx) as audio clips. Missing files are skipped without deleting originals.
    """
    duration_us = db.execute(
        'SELECT duration_us FROM timeline_revisions WHERE id=?', (tl_id,)).fetchone()['duration_us']
    cfg = _json_file(pid, 'soundtrack.json') or {}
    for key, kind in (('narration', 'narration'), ('music', 'music'), ('ambience', 'ambience')):
        file = cfg.get(key)
        if not file:
            continue
        gain_key = {'narration': 'narration_gain', 'music': 'music_gain',
                    'ambience': 'ambience_gain'}[key]
        if db.execute(
                'SELECT 1 FROM timeline_audio_clips WHERE timeline_revision_id=? AND kind=?',
                (tl_id, kind)).fetchone():
            continue
        db.execute(
            'INSERT INTO timeline_audio_clips(id,timeline_revision_id,kind,file,sha256,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,gain,fade_in_us,fade_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, tl_id, kind, str(file), _hash_file(file),
             0, duration_us, 0, duration_us, float(cfg.get(gain_key, 1.0)),
             0, 0, _now()))

    events = _json_file(pid, 'audio-events.json') or []
    for event in events:
        if not isinstance(event, dict) or not event.get('file'):
            continue
        file = event['file']
        at_us = int(round(float(event.get('at', 0)) * MICRO))
        length_us = int(round(float(event.get('duration', 0)) * MICRO))
        gain = float(event.get('gain', 1.0))
        fade_in_us = int(round(float(event.get('fade_in', 0)) * MICRO))
        fade_out_us = int(round(float(event.get('fade_out', 0)) * MICRO))
        if db.execute(
                'SELECT 1 FROM timeline_audio_clips WHERE timeline_revision_id=? AND kind=? AND timeline_in_us=?',
                (tl_id, 'sfx', at_us)).fetchone():
            continue
        db.execute(
            'INSERT INTO timeline_audio_clips(id,timeline_revision_id,kind,file,sha256,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,gain,fade_in_us,fade_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, tl_id, 'sfx', str(file), _hash_file(file),
             0, length_us, at_us, at_us + length_us, gain, fade_in_us, fade_out_us, _now()))


def _materialize_subtitles(db, pid: str, tl_id: str) -> None:
    cfg = _json_file(pid, 'soundtrack.json') or {}
    srt = cfg.get('subtitles')
    if not srt:
        return
    if db.execute('SELECT 1 FROM subtitle_revisions WHERE timeline_revision_id=?',
                  (tl_id,)).fetchone():
        return
    db.execute(
        'INSERT INTO subtitle_revisions(id,timeline_revision_id,srt_file,sha256,language,created) '
        'VALUES (?,?,?,?,NULL,?)',
        (uuid4().hex, tl_id, str(srt), _hash_file(srt), _now()))


def ensure_legacy_revisions(pid: str, db=None) -> dict:
    """Idempotently materialize one legacy storyboard + timeline revision.

    Never alters ``shots``, ``shot_assets``, media files, or ``render_history``.
    """
    db, own = _connect(db)
    try:
        return _ensure_legacy_revisions(db, pid)
    finally:
        if own:
            db.commit()


def _current(db, pid: str) -> dict:
    core.project(db, pid)
    state = _ensure_state(db, pid)
    if not state.get('current_storyboard_revision_id') or not state.get('current_timeline_revision_id'):
        _ensure_legacy_revisions(db, pid)
        state = _ensure_state(db, pid)
    storyboard_revision = db.execute(
        'SELECT * FROM storyboard_revisions WHERE id=?',
        (state['current_storyboard_revision_id'],)).fetchone()
    timeline_revision = db.execute(
        'SELECT * FROM timeline_revisions WHERE id=?',
        (state['current_timeline_revision_id'],)).fetchone()
    return {
        'project_id': pid,
        'storyboard_revision_id': state['current_storyboard_revision_id'],
        'timeline_revision_id': state['current_timeline_revision_id'],
        'storyboard_revision': dict(storyboard_revision) if storyboard_revision else None,
        'timeline_revision': dict(timeline_revision) if timeline_revision else None,
    }


def current(pid: str, db=None) -> dict:
    """Return current pointers and revisions; lazily materialize legacy state."""
    db, own = _connect(db)
    try:
        return _current(db, pid)
    finally:
        if own:
            db.commit()


def _sync_legacy_projection(db, pid: str) -> None:
    from . import timeline
    timeline.migrate(db)
    state = _ensure_state(db, pid)
    tl_id = state.get('current_timeline_revision_id')
    if not tl_id:
        tl_id = _ensure_legacy_revisions(db, pid)['timeline_revision_id']
    db.execute('DELETE FROM timeline_items WHERE project_id=?', (pid,))
    for clip in db.execute(
            'SELECT * FROM timeline_video_clips WHERE timeline_revision_id=? ORDER BY position',
            (tl_id,)).fetchall():
        duration = (clip['timeline_out_us'] - clip['timeline_in_us']) // MICRO
        db.execute(
            'INSERT INTO timeline_items(project_id,shot_id,asset_id,position,duration) '
            'VALUES (?,?,?,?,?)',
            (pid, clip['shot_id'], clip['asset_id'], clip['position'], duration))


def sync_legacy_projection(pid: str, db=None) -> None:
    """Rewrite ``timeline_items`` from the current timeline revision.

    This is the only new writer of ``timeline_items``; the legacy table is a
    compatibility projection, not truth.
    """
    db, own = _connect(db)
    try:
        _sync_legacy_projection(db, pid)
    finally:
        if own:
            db.commit()
