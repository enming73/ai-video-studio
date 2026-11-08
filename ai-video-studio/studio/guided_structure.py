"""Atomic, non-destructive storyboard structure revisions for guided projects."""
from __future__ import annotations

from uuid import uuid4

from . import core, guided_boundaries, guided_characters, guided_storyboard, guided_workflow, provider_capabilities, timeline

_UNRESOLVED_VIDEO = ('SUBMITTING', 'SUBMITTED', 'RUNNING', 'DOWNLOAD_PENDING')
_UNRESOLVED_IMAGE = ('SUBMITTING', 'UNKNOWN')


def _capabilities(db, pid, p, enabled):
    """Resolve the duration capability table for this project."""
    choice = db.execute('SELECT provider, model FROM guided_video_choices WHERE project_id=?',
                        (pid,)).fetchone()
    if choice:
        provider, model = choice['provider'], choice['model']
    else:
        provider = p['provider']
        model = guided_storyboard._MODELS[provider]
    if enabled:
        return provider, model, provider_capabilities.i2v_capabilities(provider, model)
    return provider, model, provider_capabilities.capabilities(provider, model)


def _reject_unresolved(db, pid):
    if db.execute('SELECT 1 FROM guided_attempts WHERE project_id=? AND status IN (%s)'
                  % ','.join('?' for _ in _UNRESOLVED_VIDEO), (pid, *_UNRESOLVED_VIDEO)).fetchone():
        raise ValueError('An unresolved request blocks storyboard changes')
    if db.execute('SELECT 1 FROM guided_image_attempts WHERE project_id=? AND status IN (%s)'
                  % ','.join('?' for _ in _UNRESOLVED_IMAGE), (pid, *_UNRESOLVED_IMAGE)).fetchone():
        raise ValueError('An unresolved request blocks storyboard changes')


def _shot_has_media(db, pid, shot_id):
    if db.execute('SELECT 1 FROM timeline_items WHERE project_id=? AND shot_id=?', (pid, shot_id)).fetchone():
        return True
    if db.execute("SELECT 1 FROM guided_attempts WHERE project_id=? AND shot_id=? "
                  "AND (asset_id IS NOT NULL OR status='COMPLETED')", (pid, shot_id)).fetchone():
        return True
    if db.execute('SELECT 1 FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, shot_id)).fetchone():
        return True
    return False


def _neighbors(ids, index):
    left = ids[index - 1] if index > 0 else 'START'
    right = ids[index + 1] if index < len(ids) - 1 else 'END'
    return left, right


def _copy_candidates(db, pid, new_boundary_id, left, right, old_entries):
    sources = []
    if left != 'START':
        source = next((b for b, l, _r in old_entries if l == left), None)
        if source is not None:
            sources.append(source)
    if right != 'END':
        source = next((b for b, _l, r in old_entries if r == right), None)
        if source is not None:
            sources.append(source)
    seen_sha = set()
    for source in sources:
        versions = db.execute('SELECT * FROM guided_boundary_versions WHERE project_id=? AND boundary_id=?',
                              (pid, source['id'])).fetchall()
        for version in versions:
            if version['sha256'] in seen_sha:
                continue
            seen_sha.add(version['sha256'])
            new_version_id = uuid4().hex
            db.execute('INSERT INTO guided_boundary_versions(id,project_id,boundary_id,file,sha256,stale,created) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (new_version_id, pid, new_boundary_id, version['file'], version['sha256'], 1, core.now()))
            detail = db.execute('SELECT prompt, model, created FROM guided_boundary_details WHERE frame_id=?',
                                (version['id'],)).fetchone()
            if detail:
                db.execute('INSERT INTO guided_boundary_details(frame_id,prompt,model,created) VALUES (?,?,?,?)',
                           (new_version_id, detail['prompt'], detail['model'], detail['created']))
            for ref in db.execute('SELECT character_id, image_id FROM guided_boundary_characters WHERE frame_id=?',
                                  (version['id'],)):
                db.execute('INSERT INTO guided_boundary_characters(frame_id,character_id,image_id) VALUES (?,?,?)',
                           (new_version_id, ref['character_id'], ref['image_id']))


def _rebuild_boundaries(db, pid, old_ids, new_ids):
    old_boundaries = db.execute('SELECT * FROM guided_boundaries WHERE project_id=? AND active=1 ORDER BY position',
                                (pid,)).fetchall()
    old_entries = []
    for boundary in old_boundaries:
        position = boundary['position']
        left = old_ids[position - 1] if position > 0 else 'START'
        right = old_ids[position] if position < len(old_ids) else 'END'
        old_entries.append((boundary, left, right))

    # Clear every old position first so reused positions never collide with the
    # UNIQUE(project_id, position) constraint while being reassigned.
    db.execute('UPDATE guided_boundaries SET active=0, position=NULL WHERE project_id=? AND active=1', (pid,))

    new_boundary_ids = []
    for position in range(len(new_ids) + 1):
        left = new_ids[position - 1] if position > 0 else 'START'
        right = new_ids[position] if position < len(new_ids) else 'END'
        match = next((b for b, l, r in old_entries if l == left and r == right), None)
        if match is not None:
            db.execute('UPDATE guided_boundaries SET active=1, position=? WHERE id=?', (position, match['id']))
        else:
            boundary_id = uuid4().hex
            db.execute('INSERT INTO guided_boundaries(id,project_id,position,confirmed_image_id,active,created) '
                       'VALUES (?,?,?,?,?,?)', (boundary_id, pid, position, None, 1, core.now()))
            _copy_candidates(db, pid, boundary_id, left, right, old_entries)
            new_boundary_ids.append(boundary_id)
    return new_boundary_ids


def apply(pid: str, shots: list[dict], revision: int) -> dict:
    if not isinstance(shots, list) or not shots:
        raise ValueError('Shot list required')
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError('Invalid shot')
        if not isinstance(shot.get('prompt'), str) or not shot['prompt'].strip():
            raise ValueError('Shot description required')
        if type(shot.get('duration')) is not int:
            raise ValueError('Shot duration must be an integer')
        if not isinstance(shot.get('character_ids'), list):
            raise ValueError('Shot character_ids required')
        shot_id = shot.get('id')
        if shot_id is not None and (not isinstance(shot_id, str) or not shot_id):
            raise ValueError('Invalid shot id')

    with core.connect() as db:
        timeline.migrate(db)
        db.execute('BEGIN IMMEDIATE')
        row = guided_workflow._row(db, pid)
        guided_workflow._revision(row, revision)
        if not row['storyboard_confirmed']:
            raise ValueError('Confirm storyboard first')
        project = core.project(db, pid)

        enabled = bool(db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone())
        _provider, _model, cap = _capabilities(db, pid, project, enabled)
        if not cap['verified']:
            raise ValueError('Video model duration capability is unverified')
        provider_capabilities.validate_durations(project['duration'], [s['duration'] for s in shots], cap['durations'])

        _reject_unresolved(db, pid)

        old_rows = db.execute('SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,)).fetchall()
        old_ids = [s['id'] for s in old_rows]
        old_by_id = {s['id']: s for s in old_rows}

        new_ids = []
        seen = set()
        for shot in shots:
            sid = shot.get('id') if shot.get('id') is not None else uuid4().hex
            if sid in seen:
                raise ValueError('Duplicate shot id')
            seen.add(sid)
            new_ids.append(sid)
        for shot, sid in zip(shots, new_ids):
            if shot.get('id') is not None and sid not in old_by_id:
                raise ValueError('Unknown shot id')
            if enabled:
                guided_characters._validate_ids(db, pid, shot['character_ids'])

        # Duration changes on a shot with generated media are only allowed when
        # the shot's neighbours change too (its video is invalidated anyway).
        for index, (shot, sid) in enumerate(zip(shots, new_ids)):
            if shot.get('id') is None:
                continue
            old = old_by_id[sid]
            if shot['duration'] == old['duration'] or not _shot_has_media(db, pid, sid):
                continue
            old_left, old_right = _neighbors(old_ids, old_ids.index(sid))
            new_left, new_right = _neighbors(new_ids, index)
            if old_left == new_left and old_right == new_right:
                raise ValueError('Shot has generated media; archive and rebuild it to change its duration')

        # Invalidate adjacent boundaries before the rebuild (Task 3 cascades use
        # the pre-reindex sequence) and refresh cast links that changed.
        for shot, sid in zip(shots, new_ids):
            if shot.get('id') is None:
                continue
            old = old_by_id[sid]
            prompt_changed = shot['prompt'].strip() != old['prompt']
            character_changed = False
            if enabled:
                current = {r['character_id'] for r in db.execute(
                    'SELECT character_id FROM guided_shot_characters WHERE project_id=? AND shot_id=?', (pid, sid))}
                character_changed = current != set(shot['character_ids'])
            if prompt_changed or character_changed:
                guided_boundaries.invalidate_for_shot(db, pid, sid)
            if enabled and character_changed:
                db.execute('DELETE FROM guided_shot_characters WHERE project_id=? AND shot_id=?', (pid, sid))
                for cid in shot['character_ids']:
                    db.execute('INSERT INTO guided_shot_characters VALUES (?,?,?)', (pid, sid, cid))
                db.execute('DELETE FROM guided_shot_cast_reviewed WHERE project_id=? AND shot_id=?', (pid, sid))

        # Archive missing shots; media rows and files are never removed.
        new_id_set = set(new_ids)
        archived_count = db.execute('SELECT COUNT(*) FROM shots WHERE project_id=? AND archived=1', (pid,)).fetchone()[0]
        for old_id in old_ids:
            if old_id in new_id_set:
                continue
            db.execute('UPDATE shots SET archived=1, seq=? WHERE id=?', (-1 - archived_count, old_id))
            archived_count += 1
            db.execute('DELETE FROM timeline_items WHERE project_id=? AND shot_id=?', (pid, old_id))
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?', (pid, old_id))

        # Reindex active shots 1..N, then insert new rows at fresh ids.
        db.execute('UPDATE shots SET seq = seq + 1000000 WHERE project_id=? AND archived=0', (pid,))
        for index, (shot, sid) in enumerate(zip(shots, new_ids)):
            if shot.get('id') is None:
                db.execute('INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)',
                           (sid, pid, 2000000 + index, shot['prompt'].strip(), shot['duration'], 'PLANNED'))
                if enabled:
                    for cid in shot['character_ids']:
                        db.execute('INSERT INTO guided_shot_characters VALUES (?,?,?)', (pid, sid, cid))
        for index, sid in enumerate(new_ids, 1):
            db.execute('UPDATE shots SET seq=? WHERE id=?', (index, sid))

        # Update prompts and durations for existing shots.
        for shot, sid in zip(shots, new_ids):
            if shot.get('id') is None:
                continue
            db.execute('UPDATE shots SET prompt=?, duration=? WHERE id=?',
                       (shot['prompt'].strip(), shot['duration'], sid))

        new_boundary_ids = _rebuild_boundaries(db, pid, old_ids, new_ids) if enabled else []
        for boundary_id in new_boundary_ids:
            guided_boundaries.mark_affected(db, pid, boundary_id)

        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))

    return guided_workflow.get(pid)
