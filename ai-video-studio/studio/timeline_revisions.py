"""Copy-on-write timeline revisions, video/audio clips, and subtitles.

Timeline content truth lives in ``timeline_revisions`` and its clip/subtitle
tables; ``timeline_items`` is a compatibility projection refreshed by
``edit_model.sync_legacy_projection``. Every edit clones the current revision
(copy-on-write); old revisions are immutable.

Persisted timeline times use integer microseconds (``*_us``). Audio and
subtitle edits never touch video clips or assets: they clone video clips
verbatim and only add/change audio clips or subtitle rows.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from . import core, edit_model

MICRO = edit_model.MICRO


def _connect(db):
    if db is not None:
        return db, False
    return core.connect(), True


def _ensure(db, pid):
    """Materialize legacy revisions if absent and return the current timeline
    revision id. Idempotent.

    Uses ``edit_model._current`` (which only materializes when a pointer is
    missing) rather than ``ensure_legacy_revisions`` directly, so newer
    copy-on-write revisions are never clobbered.
    """
    return edit_model._current(db, pid)['timeline_revision_id']


def _timeline(db, pid, revision_id):
    row = db.execute('SELECT * FROM timeline_revisions WHERE id=? AND project_id=?',
                     (revision_id, pid)).fetchone()
    if row is None:
        raise ValueError('Unknown timeline revision')
    return row


def _get(db, pid, revision_id, *, ensure=True):
    if ensure:
        _ensure(db, pid)
    tl = _timeline(db, pid, revision_id)
    video_clips = [dict(r) for r in db.execute(
        'SELECT * FROM timeline_video_clips WHERE timeline_revision_id=? ORDER BY position',
        (revision_id,)).fetchall()]
    audio_clips = [dict(r) for r in db.execute(
        'SELECT * FROM timeline_audio_clips WHERE timeline_revision_id=? ORDER BY timeline_in_us',
        (revision_id,)).fetchall()]
    subtitles = [dict(r) for r in db.execute(
        'SELECT * FROM subtitle_revisions WHERE timeline_revision_id=?',
        (revision_id,)).fetchall()]
    return {**dict(tl), 'video_clips': video_clips, 'audio_clips': audio_clips,
            'subtitles': subtitles}


def get(pid, revision_id, db=None):
    """Return a timeline revision with its video clips, audio clips, and subtitles."""
    db, own = _connect(db)
    try:
        return _get(db, pid, revision_id)
    finally:
        if own:
            db.commit()


def current(pid, db=None):
    """Return the current timeline revision, lazily materializing legacy state."""
    db, own = _connect(db)
    try:
        rid = _ensure(db, pid)
        return _get(db, pid, rid, ensure=False)
    finally:
        if own:
            db.commit()


def list_revisions(pid, db=None):
    """List timeline revisions newest-first."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        return [dict(r) for r in db.execute(
            'SELECT * FROM timeline_revisions WHERE project_id=? ORDER BY revision_no DESC',
            (pid,)).fetchall()]
    finally:
        if own:
            db.commit()


def _next_revision_no(db, pid):
    row = db.execute('SELECT MAX(revision_no) FROM timeline_revisions WHERE project_id=?',
                     (pid,)).fetchone()
    return (row[0] or 0) + 1


def _clone(db, pid, base_id, *, source, actor):
    """Clone ``base_id`` into a new revision (copy-on-write).

    Copies the base revision row plus all video clips, audio clips, and
    subtitles. Does not advance ``project_state``: callers promote only after
    validation succeeds.
    """
    base = _timeline(db, pid, base_id)
    new_id = uuid4().hex
    db.execute(
        'INSERT INTO timeline_revisions(id,project_id,storyboard_revision_id,revision_no,fps,duration_us,source,actor,created) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (new_id, pid, base['storyboard_revision_id'], _next_revision_no(db, pid),
         base['fps'], base['duration_us'], source, actor, core.now()))
    for clip in db.execute(
            'SELECT * FROM timeline_video_clips WHERE timeline_revision_id=?', (base_id,)).fetchall():
        db.execute(
            'INSERT INTO timeline_video_clips(id,timeline_revision_id,shot_id,asset_id,position,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, new_id, clip['shot_id'], clip['asset_id'], clip['position'],
             clip['source_in_us'], clip['source_out_us'], clip['timeline_in_us'],
             clip['timeline_out_us'], core.now()))
    for clip in db.execute(
            'SELECT * FROM timeline_audio_clips WHERE timeline_revision_id=?', (base_id,)).fetchall():
        db.execute(
            'INSERT INTO timeline_audio_clips(id,timeline_revision_id,kind,file,sha256,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,gain,fade_in_us,fade_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, new_id, clip['kind'], clip['file'], clip['sha256'],
             clip['source_in_us'], clip['source_out_us'], clip['timeline_in_us'],
             clip['timeline_out_us'], clip['gain'], clip['fade_in_us'], clip['fade_out_us'],
             core.now()))
    for sub in db.execute(
            'SELECT * FROM subtitle_revisions WHERE timeline_revision_id=?', (base_id,)).fetchall():
        db.execute(
            'INSERT INTO subtitle_revisions(id,timeline_revision_id,srt_file,sha256,language,created) '
            'VALUES (?,?,?,?,?,?)',
            (uuid4().hex, new_id, sub['srt_file'], sub['sha256'], sub['language'], core.now()))
    return new_id


def _promote(db, pid, new_id):
    db.execute('UPDATE project_state SET current_timeline_revision_id=?, updated=? WHERE project_id=?',
               (new_id, core.now(), pid))


def _asset_duration_us(db, asset_id):
    """Best-effort asset duration in microseconds (probe, else shot-duration fallback)."""
    asset = db.execute('SELECT * FROM shot_assets WHERE id=?', (asset_id,)).fetchone()
    if asset is None:
        return None
    shot = db.execute('SELECT duration FROM shots WHERE id=?', (asset['shot_id'],)).fetchone()
    fallback = (shot['duration'] * MICRO) if shot else 0
    return edit_model._probe_duration_us(asset['file']) or fallback


def _validate_clips(db, clips):
    """Return a list of error dicts for a would-be video-clip set.

    ``clips`` is a list of dicts with at least ``shot_id``, ``asset_id``,
    ``position``, ``source_in_us``, ``source_out_us``, ``timeline_in_us`` and
    ``timeline_out_us``. Checks missing assets, source overflow, duration
    consistency, and positional gaps/overlaps.
    """
    errors = []
    for c in clips:
        asset = db.execute('SELECT * FROM shot_assets WHERE id=?', (c['asset_id'],)).fetchone()
        if asset is None:
            errors.append({'type': 'missing_asset', 'shot_id': c['shot_id'],
                           'message': f"missing asset {c['asset_id']}"})
            continue
        if c['source_in_us'] < 0 or c['source_out_us'] <= c['source_in_us']:
            errors.append({'type': 'invalid_duration', 'shot_id': c['shot_id'],
                           'message': f"invalid duration: source {c['source_in_us']}..{c['source_out_us']}"})
        else:
            asset_dur = _asset_duration_us(db, c['asset_id'])
            if asset_dur is not None and c['source_out_us'] > asset_dur:
                errors.append({'type': 'source_overflow', 'shot_id': c['shot_id'],
                               'message': f"source exceeds asset {c['asset_id']}: "
                                          f"{c['source_out_us']}us > {asset_dur}us"})
        if (c['timeline_out_us'] - c['timeline_in_us']) != (c['source_out_us'] - c['source_in_us']):
            errors.append({'type': 'invalid_duration', 'shot_id': c['shot_id'],
                           'message': f"invalid duration: timeline {c['timeline_in_us']}.."
                                      f"{c['timeline_out_us']} != source {c['source_in_us']}..{c['source_out_us']}"})
    ordered = sorted(clips, key=lambda c: c['position'])
    prev = None
    for c in ordered:
        if prev is not None:
            if c['position'] == prev['position']:
                errors.append({'type': 'overlap', 'shot_id': c['shot_id'],
                               'message': f"overlap at position {c['position']}"})
            elif c['timeline_in_us'] < prev['timeline_out_us']:
                errors.append({'type': 'overlap', 'shot_id': c['shot_id'],
                               'message': f"overlap between shot {prev['shot_id']} and shot {c['shot_id']}"})
            elif c['timeline_in_us'] > prev['timeline_out_us']:
                errors.append({'type': 'gap', 'shot_id': c['shot_id'],
                               'message': f"gap between shot {prev['shot_id']} and shot {c['shot_id']}"})
        prev = c
    if ordered and ordered[0]['timeline_in_us'] > 0:
        errors.append({'type': 'gap', 'shot_id': ordered[0]['shot_id'],
                       'message': 'gap at start of timeline'})
    return errors


def validate(pid, revision_id, db=None):
    """Return validation errors for ``revision_id`` (empty list when valid)."""
    db, own = _connect(db)
    try:
        _timeline(db, pid, revision_id)
        clips = [dict(r) for r in db.execute(
            'SELECT * FROM timeline_video_clips WHERE timeline_revision_id=?',
            (revision_id,)).fetchall()]
        return _validate_clips(db, clips)
    finally:
        if own:
            db.commit()


def _set_clip_in_revision(db, new_id, shot_id, asset_id, source_in_us, source_out_us,
                          position, timeline_in_us, timeline_out_us):
    db.execute('DELETE FROM timeline_video_clips WHERE timeline_revision_id=? AND shot_id=?',
               (new_id, shot_id))
    db.execute(
        'INSERT INTO timeline_video_clips(id,timeline_revision_id,shot_id,asset_id,position,'
        'source_in_us,source_out_us,timeline_in_us,timeline_out_us,created) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uuid4().hex, new_id, shot_id, asset_id, position, source_in_us, source_out_us,
         timeline_in_us, timeline_out_us, core.now()))


def set_video_clip(pid, revision_id, shot_id, asset_id, *, source_in_us, source_out_us,
                   position, actor='human', db=None):
    """Copy-on-write: replace/insert the video clip for ``shot_id``.

    Validates the would-be revision (source overflow, gaps, overlaps, invalid
    duration, missing assets) before writing; raises ``ValueError`` on the first
    error.
    """
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        _timeline(db, pid, revision_id)
        source = 'manual' if actor == 'human' else 'automatic'
        asset = db.execute(
            'SELECT * FROM shot_assets WHERE id=? AND project_id=? AND shot_id=?',
            (asset_id, pid, shot_id)).fetchone()
        if asset is None:
            raise ValueError(f'missing asset {asset_id}')
        if not isinstance(position, int) or position < 1:
            raise ValueError('position must be a positive integer')
        if not isinstance(source_in_us, int) or not isinstance(source_out_us, int) \
                or source_in_us < 0 or source_out_us <= source_in_us:
            raise ValueError(f'invalid duration: source {source_in_us}..{source_out_us}')
        asset_dur = _asset_duration_us(db, asset_id)
        if asset_dur is not None and source_out_us > asset_dur:
            raise ValueError(f'source exceeds asset {asset_id}: {source_out_us}us > {asset_dur}us')

        base_clips = [dict(r) for r in db.execute(
            'SELECT * FROM timeline_video_clips WHERE timeline_revision_id=?',
            (revision_id,)).fetchall()]
        others = [c for c in base_clips if c['shot_id'] != shot_id]
        duration_us = source_out_us - source_in_us
        preceding = [c for c in others if c['position'] < position]
        timeline_in_us = max((c['timeline_out_us'] for c in preceding), default=0)
        timeline_out_us = timeline_in_us + duration_us
        would_be = others + [{
            'shot_id': shot_id, 'asset_id': asset_id, 'position': position,
            'source_in_us': source_in_us, 'source_out_us': source_out_us,
            'timeline_in_us': timeline_in_us, 'timeline_out_us': timeline_out_us,
        }]
        errors = _validate_clips(db, would_be)
        if errors:
            raise ValueError(errors[0]['message'])

        new_id = _clone(db, pid, revision_id, source=source, actor=actor)
        _set_clip_in_revision(db, new_id, shot_id, asset_id, source_in_us, source_out_us,
                              position, timeline_in_us, timeline_out_us)
        _promote(db, pid, new_id)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def add_audio_clip(pid, revision_id, *, kind, file, source_out_us, source_in_us=0,
                   timeline_in_us=0, timeline_out_us=None, gain=1.0, fade_in_us=0,
                   fade_out_us=0, actor='human', db=None):
    """Copy-on-write: add an audio clip. Video clips and assets are untouched."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        _timeline(db, pid, revision_id)
        path = Path(file).expanduser().resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Missing or empty audio file: {path}')
        if timeline_out_us is None:
            timeline_out_us = timeline_in_us + (source_out_us - source_in_us)
        source = 'manual' if actor == 'human' else 'automatic'
        new_id = _clone(db, pid, revision_id, source=source, actor=actor)
        db.execute(
            'INSERT INTO timeline_audio_clips(id,timeline_revision_id,kind,file,sha256,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,gain,fade_in_us,fade_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, new_id, kind, str(path), edit_model._hash_file(str(path)),
             source_in_us, source_out_us, timeline_in_us, timeline_out_us,
             float(gain), int(fade_in_us), int(fade_out_us), core.now()))
        _promote(db, pid, new_id)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def set_subtitles(pid, revision_id, srt_file, *, language=None, actor='human', db=None):
    """Copy-on-write: set the subtitle revision. Video/audio clips are untouched."""
    db, own = _connect(db)
    try:
        _ensure(db, pid)
        _timeline(db, pid, revision_id)
        path = Path(srt_file).expanduser().resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Missing or empty subtitle file: {path}')
        source = 'manual' if actor == 'human' else 'automatic'
        new_id = _clone(db, pid, revision_id, source=source, actor=actor)
        db.execute('DELETE FROM subtitle_revisions WHERE timeline_revision_id=?', (new_id,))
        db.execute(
            'INSERT INTO subtitle_revisions(id,timeline_revision_id,srt_file,sha256,language,created) '
            'VALUES (?,?,?,?,?,?)',
            (uuid4().hex, new_id, str(path), edit_model._hash_file(str(path)), language, core.now()))
        _promote(db, pid, new_id)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def create_draft(pid, *, actor='human', db=None):
    """Create a draft revision cloning the current one with no changes."""
    db, own = _connect(db)
    try:
        rid = _ensure(db, pid)
        source = 'manual' if actor == 'human' else 'automatic'
        new_id = _clone(db, pid, rid, source=source, actor=actor)
        _promote(db, pid, new_id)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()


def sync_audio_subtitles(pid, *, actor='human', db=None):
    """Re-import legacy sound config/events into a copy-on-write revision.

    Preserves video clips and assets; replaces audio clips (narration/music/
    ambience/sfx) and the subtitle revision from the current JSON sources.
    This is the sound-edit integration point (``sound.py``).
    """
    db, own = _connect(db)
    try:
        rid = _ensure(db, pid)
        source = 'manual' if actor == 'human' else 'automatic'
        new_id = _clone(db, pid, rid, source=source, actor=actor)
        db.execute('DELETE FROM timeline_audio_clips WHERE timeline_revision_id=?', (new_id,))
        db.execute('DELETE FROM subtitle_revisions WHERE timeline_revision_id=?', (new_id,))
        edit_model._materialize_audio_clips(db, pid, new_id)
        edit_model._materialize_subtitles(db, pid, new_id)
        _promote(db, pid, new_id)
        return _get(db, pid, new_id, ensure=False)
    finally:
        if own:
            db.commit()
