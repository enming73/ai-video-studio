"""Immutable render snapshots: freeze a timeline revision and re-export it
reproducibly without rereading mutable project state.

A snapshot pins the exact video clips, audio clips and subtitles of a timeline
revision, together with their input hashes and the frozen soundtrack/audio
config. Once frozen, ``reexport`` renders from the manifest alone and never
touches the current revision, so a later edit cannot change what a snapshot
produces. ``shots.id`` is stable; snapshots are append-only and old revisions
are immutable.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from uuid import uuid4

from . import core, edit_model, edit_readiness, timeline, timeline_revisions

FPS = 24
WIDTH = 1280
HEIGHT = 720
VIDEO_CODEC = 'libx264'
AUDIO_CODEC = 'aac'
SNAPSHOT_STATUSES = ('PENDING', 'READY', 'FAILED')


def _connect(db):
    if db is not None:
        return db, False
    return core.connect(), True


def _snapshot_row(db, pid, snapshot_id):
    row = db.execute('SELECT * FROM render_snapshots WHERE id=? AND project_id=?',
                     (snapshot_id, pid)).fetchone()
    if row is None:
        raise ValueError('Unknown render snapshot')
    return dict(row)


def _freeze_manifest(db, pid, rid):
    """Freeze a timeline revision's clips/audio/subtitles into a manifest.

    Includes the frozen soundtrack/audio config so ``sound.export`` can mix from
    the snapshot rather than rereading the mutable JSON files.
    """
    tl = timeline_revisions.get(pid, rid, db=db)
    video_clips = []
    for c in tl['video_clips']:
        asset = db.execute('SELECT * FROM shot_assets WHERE id=?',
                           (c['asset_id'],)).fetchone()
        shot = db.execute('SELECT seq FROM shots WHERE id=?', (c['shot_id'],)).fetchone()
        video_clips.append({
            'shot_id': c['shot_id'],
            'seq': shot['seq'] if shot else None,
            'asset_id': c['asset_id'],
            'source': asset['source'] if asset else None,
            'file': asset['file'] if asset else None,
            'sha256': asset['sha256'] if asset else None,
            'position': c['position'],
            'source_in_us': c['source_in_us'],
            'source_out_us': c['source_out_us'],
            'timeline_in_us': c['timeline_in_us'],
            'timeline_out_us': c['timeline_out_us'],
        })
    audio_clips = [dict(a) for a in tl['audio_clips']]
    subtitle = None
    subtitle_revision_id = None
    if tl['subtitles']:
        s = tl['subtitles'][0]
        subtitle = {'srt_file': s['srt_file'], 'sha256': s['sha256'],
                    'language': s['language']}
        subtitle_revision_id = s['id']
    return {
        'project_id': pid,
        'timeline_revision_id': rid,
        'storyboard_revision_id': tl['storyboard_revision_id'],
        'subtitle_revision_id': subtitle_revision_id,
        'fps': FPS,
        'width': WIDTH,
        'height': HEIGHT,
        'video_codec': VIDEO_CODEC,
        'audio_codec': AUDIO_CODEC,
        'duration_us': tl['duration_us'],
        'video_clips': video_clips,
        'audio_clips': audio_clips,
        'subtitle': subtitle,
        'config': edit_model._json_file(pid, 'soundtrack.json'),
        'audio_events': edit_model._json_file(pid, 'audio-events.json') or [],
    }


def _require_renderable(pid, *, db):
    """Reject snapshots that would silently render stale or blocked edits.

    Blocks on any shot that is ``blocked`` (no selected asset) or ``stale``
    (decision-ledger or legacy invalidation). It deliberately does not require a
    per-shot decision: that policy is guided-only and is enforced separately by
    the guided confirmation path.
    """
    guided = db.execute('SELECT * FROM guided_projects WHERE project_id=?',
                        (pid,)).fetchone()
    mode = 'guided' if guided else 'automatic'
    if guided:
        if not guided['storyboard_confirmed']:
            raise ValueError('Guided render requires a confirmed storyboard and selected assets')
        from . import guided_characters
        guided_characters.require_render_ready(db, pid)
        for shot in db.execute(
                'SELECT * FROM shots WHERE project_id=? AND archived=0 ORDER BY seq',
                (pid,)).fetchall():
            approval = db.execute(
                'SELECT 1 FROM guided_confirmations c '
                'JOIN timeline_items t ON t.project_id=c.project_id AND t.shot_id=c.shot_id '
                'WHERE c.project_id=? AND c.shot_id=? AND c.asset_id=t.asset_id AND c.prompt=?',
                (pid, shot['id'], shot['prompt'])).fetchone()
            if not approval:
                raise ValueError('All guided shots must be confirmed before render')
    state = edit_readiness.status(pid, mode=mode, db=db)
    blockers = []
    for shot in state['shots']:
        if shot['status'] == 'blocked':
            blockers.append(f"shot {shot['seq']} has no selected asset")
        elif shot['status'] == 'stale':
            blockers.append(f"shot {shot['seq']} is stale: {'; '.join(shot['reasons'])}")
    if blockers:
        raise ValueError('Cannot render stale edits: ' + '; '.join(blockers))
    return state


def create(pid, *, timeline_revision_id=None, actor='human'):
    """Freeze the current (or given) timeline revision as a ``PENDING`` snapshot
    and enqueue a ``local_render`` job. Rejects stale or blocked edits."""
    from . import jobs, ledger
    db, own = _connect(None)
    try:
        core.project(db, pid)
        rid = timeline_revision_id or timeline_revisions.current(pid, db=db)['id']
        timeline_revisions._timeline(db, pid, rid)
        _require_renderable(pid, db=db)

        manifest = _freeze_manifest(db, pid, rid)
        snapshot_id = uuid4().hex
        db.execute(
            'INSERT INTO render_snapshots(id,project_id,timeline_revision_id,'
            'storyboard_revision_id,subtitle_revision_id,status,manifest_json,'
            'fps,width,height,video_codec,audio_codec,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (snapshot_id, pid, rid, manifest['storyboard_revision_id'],
             manifest['subtitle_revision_id'], 'PENDING',
             json.dumps(manifest, ensure_ascii=False, sort_keys=True),
             FPS, WIDTH, HEIGHT, VIDEO_CODEC, AUDIO_CODEC, core.now()))
        run_id = ledger.open_run(pid, budget_microusd=0, label='local_render', db=db)
        jobs.create(pid, kind='local_render', payload={'snapshot_id': snapshot_id},
                    request_key=f'render-snapshot:{snapshot_id}', run_id=run_id, db=db)
        snap = _snapshot_row(db, pid, snapshot_id)
        snap['manifest'] = json.loads(snap['manifest_json'])
        return snap
    finally:
        if own:
            db.commit()


def get(pid, snapshot_id, db=None):
    """Return a snapshot row with its parsed ``manifest``."""
    db, own = _connect(db)
    try:
        core.project(db, pid)
        snap = _snapshot_row(db, pid, snapshot_id)
        snap['manifest'] = json.loads(snap['manifest_json'])
        return snap
    finally:
        if own:
            db.commit()


def manifest(pid, snapshot_id):
    """Return the frozen manifest of a snapshot."""
    return get(pid, snapshot_id)['manifest']


def _job_id(pid, snapshot_id):
    with core.connect() as db:
        row = db.execute(
            'SELECT id FROM jobs WHERE project_id=? AND request_key=?',
            (pid, f'render-snapshot:{snapshot_id}')).fetchone()
        return row['id'] if row else None


def run_and_wait(pid, snapshot_id, *, timeout=None):
    """Process the queued ``local_render`` job via the worker and return the
    snapshot once it reaches a terminal status."""
    from . import job_runner, worker
    from .handlers import register_all
    if not worker.HANDLERS:
        register_all()
    job_id = _job_id(pid, snapshot_id)
    if job_id:
        job_runner.run_until([job_id], timeout=timeout)
    else:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            snap = get(pid, snapshot_id)
            if snap['status'] in ('READY', 'FAILED'):
                return snap
            if worker.run_once() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    return snap
                time.sleep(0.05)
    return get(pid, snapshot_id)


def _mark_ready(pid, snapshot_id, output):
    with core.connect() as db:
        db.execute(
            "UPDATE render_snapshots SET status='READY', output=?, ready=?, error=NULL "
            'WHERE id=? AND project_id=?', (output, core.now(), snapshot_id, pid))


def _mark_failed(pid, snapshot_id, error):
    with core.connect() as db:
        db.execute(
            "UPDATE render_snapshots SET status='FAILED', error=? "
            'WHERE id=? AND project_id=?', (error, snapshot_id, pid))


def _legacy_history_manifest(manifest, output):
    """Render-history manifest shape (``shots`` with ``seq``/``asset_id``)."""
    return {
        'project_id': manifest['project_id'],
        'source': 'selected',
        'fps': manifest['fps'],
        'output': str(output),
        'shots': [
            {'seq': c['seq'], 'asset_id': c['asset_id'], 'source': c['source'],
             'file': c['file'], 'sha256': c['sha256'],
             'duration': (c['timeline_out_us'] - c['timeline_in_us']) // 1_000_000}
            for c in manifest['video_clips']
        ],
    }


def record_history(pid, snapshot_id, manifest, output):
    """Append a legacy ``render_history`` row for a completed snapshot render."""
    with core.connect() as db:
        timeline.migrate(db)
        db.execute(
            'INSERT INTO render_history(id,project_id,output,manifest_json,created) '
            'VALUES (?,?,?,?,?)',
            (snapshot_id, pid, str(output),
             json.dumps(_legacy_history_manifest(manifest, output)), core.now()))


def _render_video(manifest, output):
    """Render the frozen video clips to ``output`` (never overwriting)."""
    output = Path(output)
    if output.exists():
        raise ValueError(f'Output exists; refusing overwrite: {output}')
    core.require_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    fps = manifest['fps']
    width = manifest['width']
    height = manifest['height']
    work = output.parent / f'.work-{uuid4().hex}'
    work.mkdir(parents=True, exist_ok=False)
    paths = []
    try:
        for clip in manifest['video_clips']:
            src = Path(clip['file'])
            if not src.is_file() or timeline._hash(src) != clip['sha256']:
                raise ValueError(f'Asset missing or changed for shot {clip["seq"]}: {src}')
            window_us = clip['timeline_out_us'] - clip['timeline_in_us']
            duration = window_us / 1_000_000.0
            dst = work / f"shot-{clip['position']:03d}.mp4"
            vf = (f'fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,'
                  f'tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},'
                  f'setpts=N/({fps}*TB)')
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
            if clip['source_in_us']:
                cmd += ['-ss', str(clip['source_in_us'] / 1_000_000.0)]
            cmd += ['-i', str(src), '-vf', vf, '-frames:v', str(int(round(duration * fps))),
                    '-an', '-c:v', 'libx264', '-preset', 'veryfast',
                    '-pix_fmt', 'yuv420p', str(dst)]
            core.run(cmd)
            core.verify_video(dst, duration)
            paths.append(dst)
        concat = work / 'concat.txt'
        concat.write_text(''.join("file '" + str(x.resolve()).replace("'", "'\\''") + "'\n"
                                  for x in paths), encoding='utf-8')
        total = manifest['duration_us'] / 1_000_000.0
        temp = work / 'final.mp4'
        core.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                  '-f', 'concat', '-safe', '0', '-i', str(concat),
                  '-vf', f'fps={fps},setpts=N/({fps}*TB)',
                  '-frames:v', str(int(round(total * fps))), '-an',
                  '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                  '-movflags', '+faststart', str(temp)])
        core.verify_video(temp, total)
        temp.replace(output)
        output.with_suffix('.json').write_text(
            json.dumps(_legacy_history_manifest(manifest, output), indent=2,
                       ensure_ascii=False), encoding='utf-8')
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def reexport(pid, snapshot_id, *, output=None):
    """Render a snapshot's frozen manifest to a new output path.

    Reads only the frozen manifest (never the current revisions), so a later
    edit cannot change what this snapshot produces.
    """
    snap = get(pid, snapshot_id)
    manifest = snap['manifest']
    if output is None:
        output = core.HOME / pid / 'renders' / snapshot_id / f'{uuid4().hex}.mp4'
    else:
        output = Path(output).expanduser().resolve()
    _render_video(manifest, output)
    return {'snapshot_id': snapshot_id, 'output': str(output), 'manifest': manifest}
