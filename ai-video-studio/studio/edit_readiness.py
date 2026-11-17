"""Dependency invalidation and mode-aware readiness for the M2 edit model.

``edit_readiness`` is the shared validity layer that both guided and automatic
production modes consult. Edits record *invalidation* decisions (``action ==
'invalidate'``) that name the downstream domains they affect; ``status`` replays
that ledger against the current storyboard/timeline revisions and reports, per
shot, ``valid | stale | blocked`` plus ``ready`` and the decision requirements.

Dependency edges (edited node -> affected domains):

* shot prompt/duration  -> that shot's asset validity ('video') and export
* character/reference   -> affected shot asset validity ('video') only
* boundary              -> adjacent shot asset validity ('video') only
* storyboard/order      -> timeline positions ('export') only
* clip/asset selection  -> render ('export') only
* audio                 -> mix and export ('mix' + 'export')
* subtitle              -> export only
* render snapshot       -> never invalidated by later edits

The shared check is mode-independent: ``video_stale``/``mix_stale``/
``export_stale`` reflect unresolved invalidations, and ``valid|stale|blocked``
reflects technical state (missing asset blocks; an unresolved 'video'
invalidation makes a shot stale). The policy layer differs only in which actor
satisfies a shot's decision requirement: guided mode requires a ``human``
decision at the declared shot node; automatic mode accepts ``system`` or
``human`` but never fabricates a human decision. Rejections and legacy
materialization are never recorded as decisions here; old revisions and assets
are never deleted.
"""
from __future__ import annotations

import json
from uuid import uuid4

from . import core

# Actions that satisfy a shot's decision requirement (any actor per policy).
_DECISION_ACTIONS = ('accept', 'confirm', 'select', 'keep_old')
# Actions that resolve a shot's 'video' invalidation (asset-level outcomes).
_RESOLVE_ACTIONS = ('confirm', 'select', 'keep_old')


def _connect(db):
    if db is not None:
        return db, False
    return core.connect(), True


def _actor_ok(mode, actor):
    """True when ``actor`` satisfies the decision policy for ``mode``."""
    if mode == 'guided':
        return actor == 'human'
    # automatic: system or human; a system decision is never promoted to human.
    return actor in ('system', 'human')


def _domains_and_shots(db, pid, node_type, node_id):
    """Map an edited node to the invalidation domains and affected shot ids."""
    if node_type == 'shot':
        # prompt/duration change: asset validity + timeline/export.
        return ['video', 'export'], [node_id]
    if node_type == 'reference':
        # cast/reference change: asset validity only.
        return ['video'], [node_id]
    if node_type == 'character':
        shot_ids = [r['shot_id'] for r in db.execute(
            'SELECT shot_id FROM guided_shot_characters WHERE project_id=? AND character_id=?',
            (pid, node_id)).fetchall()]
        return ['video'], shot_ids
    if node_type == 'boundary':
        boundary = db.execute(
            'SELECT position FROM guided_boundaries WHERE id=? AND project_id=?',
            (node_id, pid)).fetchone()
        if not boundary or boundary['position'] is None:
            return ['video'], []
        position = boundary['position']
        ids = [r['id'] for r in db.execute(
            'SELECT id FROM shots WHERE project_id=? AND archived=0 ORDER BY seq',
            (pid,)).fetchall()]
        return ['video'], [ids[i] for i in (position - 1, position) if 0 <= i < len(ids)]
    if node_type in ('storyboard', 'order'):
        return ['export'], []
    if node_type in ('clip', 'asset'):
        return ['export'], []
    if node_type == 'audio':
        return ['mix', 'export'], []
    if node_type == 'subtitle':
        return ['export'], []
    raise ValueError(f'Unknown invalidation node type: {node_type}')


def invalidate(pid, *, node_type, node_id, reason, db=None):
    """Record an invalidation for an edited node and return affected shot ids.

    The invalidation is stored as a ``decisions`` row (action ``invalidate``,
    actor ``system``) whose metadata carries the affected ``domains`` and
    ``shot_ids``. Nothing is deleted; old revisions and assets are untouched.
    """
    db, own = _connect(db)
    try:
        core.project(db, pid)
        domains, shot_ids = _domains_and_shots(db, pid, node_type, node_id)
        if domains:
            db.execute(
                'INSERT INTO decisions(id,project_id,node_type,node_id,action,actor,metadata_json,created) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (uuid4().hex, pid, node_type, node_id, 'invalidate', 'system',
                 json.dumps({'reason': reason, 'domains': domains, 'shot_ids': shot_ids}),
                 core.now()))
        return shot_ids
    finally:
        if own:
            db.commit()


def _replay(decisions, mode):
    """Replay the decision ledger into current per-shot video state and reasons."""
    from . import storyboard_revisions  # noqa: F401  (import for module availability)

    stale = {}       # shot_id -> bool
    reasons = {}     # shot_id -> [reason, ...]
    mix_stale = False
    export_stale = False
    for d in decisions:
        if d['action'] == 'invalidate':
            meta = json.loads(d['metadata_json'] or '{}')
            domains = meta.get('domains', [])
            shot_ids = meta.get('shot_ids', [])
            reason = meta.get('reason', '')
            for domain in domains:
                if domain == 'video':
                    for sid in shot_ids:
                        stale[sid] = True
                        reasons.setdefault(sid, []).append(reason)
                elif domain == 'mix':
                    mix_stale = True
                elif domain == 'export':
                    export_stale = True
        elif d['node_type'] == 'shot' and d['action'] in _RESOLVE_ACTIONS \
                and _actor_ok(mode, d['actor']):
            stale[d['node_id']] = False
            reasons.pop(d['node_id'], None)
    return stale, reasons, mix_stale, export_stale


def status(pid, *, mode='guided', db=None):
    """Return per-shot validity, decision requirements, and top-level flags."""
    from . import storyboard_revisions, timeline_revisions

    db, own = _connect(db)
    try:
        sb = storyboard_revisions.current(pid, db=db)
        tl = timeline_revisions.current(pid, db=db)
        shots = sb['shots']
        clips = {c['shot_id']: c for c in tl['video_clips']}
        decisions = sb['decisions']

        stale, reasons, mix_stale, export_stale = _replay(decisions, mode)

        shot_results = []
        for s in shots:
            sid = s['shot_id']
            has_clip = sid in clips
            is_stale = stale.get(sid, False)
            has_decision = any(
                d['node_type'] == 'shot' and d['node_id'] == sid
                and d['action'] in _DECISION_ACTIONS and _actor_ok(mode, d['actor'])
                for d in decisions)
            if not has_clip:
                shot_status = 'blocked'
            elif is_stale:
                shot_status = 'stale'
            else:
                shot_status = 'valid'
            decisions_required = []
            if not has_decision:
                decisions_required.append(
                    {'node': 'shot', 'actor': 'human' if mode == 'guided' else 'system'})
            shot_results.append({
                'shot_id': sid,
                'seq': s['seq'],
                'status': shot_status,
                'ready': shot_status == 'valid' and has_decision,
                'reasons': reasons.get(sid, []),
                'decisions_required': decisions_required,
            })

        video_stale = any(r['status'] == 'stale' for r in shot_results)
        return {
            'project_id': pid,
            'mode': mode,
            'video_stale': video_stale,
            'mix_stale': mix_stale,
            'export_stale': export_stale,
            'shots': shot_results,
            'decisions': decisions,
        }
    finally:
        if own:
            db.commit()


def require_ready(pid, *, mode='guided', shot_ids=None, db=None):
    """Raise a clear error listing every blocker, or return the status dict."""
    state = status(pid, mode=mode, db=db)
    blockers = []
    for shot in state['shots']:
        if shot_ids is not None and shot['shot_id'] not in shot_ids:
            continue
        if shot['status'] == 'blocked':
            blockers.append(f"shot {shot['seq']} has no selected asset")
        elif shot['status'] == 'stale':
            blockers.append(
                f"shot {shot['seq']} is stale: {'; '.join(shot['reasons'])}")
        elif not shot['ready']:
            actor = 'human' if mode == 'guided' else 'system'
            blockers.append(f"shot {shot['seq']} requires a {actor} decision")
    if blockers:
        raise ValueError('Not ready: ' + '; '.join(blockers))
    return state
