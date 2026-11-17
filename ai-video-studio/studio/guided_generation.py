"""One immutable output and one durable submission record per guided attempt."""
import math
import json
import hashlib
import mimetypes
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from . import core, timeline, guided_workflow, guided_storyboard, provider_capabilities, guided_characters, guided_images, guided_boundaries
from . import attempts as attempt_store, guided_compat, job_runner, jobs, ledger, worker

_RATES = {'minimax': 'MINIMAX_USD_PER_SECOND', 'wan': 'WAN_USD_PER_SECOND', 'flux': 'FAL_USD_PER_SECOND'}
_ACTIVE = ('SUBMITTING', 'SUBMITTED', 'RUNNING', 'DOWNLOAD_PENDING')


def _shot(db, pid, seq):
    workflow = guided_workflow._row(db, pid)
    if not workflow['storyboard_confirmed']:
        raise ValueError('Confirm storyboard first')
    shot = db.execute('SELECT * FROM shots WHERE project_id=? AND seq=?', (pid, seq)).fetchone()
    if not shot:
        raise ValueError('Unknown shot')
    return dict(shot)


def _rate(provider):
    load_dotenv()
    if provider == 'mock':
        return 0.0
    try:
        value = float(os.environ[_RATES[provider]])
    except (KeyError, ValueError):
        raise ValueError('Set a verified provider USD-per-second rate') from None
    if not math.isfinite(value) or not 0 < value < 100:
        raise ValueError('Invalid provider rate')
    return value


def preview(pid: str, seq: int) -> dict:
    with core.connect() as db:
        p = dict(core.project(db, pid))
        shot = _shot(db, pid, seq)
        enabled = db.execute('SELECT * FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone()
        choice = db.execute('SELECT * FROM guided_video_choices WHERE project_id=?', (pid,)).fetchone()
        first_frame = last_frame = None
        if enabled:
            guided_characters.require_confirmed(db, pid)
            if not enabled['storyboard_reconfirmed']: raise ValueError('Confirm storyboard first')
            start, end = guided_boundaries.for_shot(db, pid, shot['id'])
            if not guided_boundaries.ready(db, pid, start['id']) or not guided_boundaries.ready(db, pid, end['id']):
                raise ValueError('Confirm both boundary frames before video generation')
            if not choice: raise ValueError('Select a supported image-to-video model')
            first_frame = db.execute('SELECT id, sha256 FROM guided_boundary_versions WHERE id=?',
                                     (start['confirmed_image_id'],)).fetchone()
            last_frame = db.execute('SELECT id, sha256 FROM guided_boundary_versions WHERE id=?',
                                    (end['confirmed_image_id'],)).fetchone()
        provider = choice['provider'] if enabled else p['provider']
        model = choice['model'] if enabled else guided_storyboard._MODELS[provider]
    if first_frame:
        first_path = guided_images.image_path(pid, first_frame['id'])
        if provider == 'minimax':
            width,height = guided_images.image_dimensions(first_path)
            if not (256 <= width <= 5760 and 256 <= height <= 5760 and 0.4 <= width / height <= 2.5):
                raise ValueError('First-frame image dimensions are unsupported by MiniMax')
    cap = provider_capabilities.i2v_capabilities(provider, model) if enabled else provider_capabilities.capabilities(provider, model)
    if not cap['verified'] or shot['duration'] not in cap['durations']:
        raise ValueError('Provider duration capability unverified or unsupported')
    if enabled and not cap.get('first_last_frame'):
        raise ValueError('Provider does not support first and last boundary frames')
    rate = _rate(provider)
    return {'project_id': pid, 'seq': seq, 'duration': shot['duration'], 'prompt': shot['prompt'],
            'provider': provider, 'model': model,
            'first_frame_id': first_frame['id'] if first_frame else None,
            'first_frame_sha256': first_frame['sha256'] if first_frame else None,
            'last_frame_id': last_frame['id'] if last_frame else None,
            'last_frame_sha256': last_frame['sha256'] if last_frame else None,
            'paid': provider != 'mock', 'estimated_cost_usd': round(rate * shot['duration'], 6),
            'note': 'Estimated reservation is not a provider-enforced spending cap.'}


def set_video_model(pid: str, provider: str, model: str, revision: int) -> dict:
    cap = provider_capabilities.i2v_capabilities(provider, model)
    if not cap['verified']: raise ValueError('Image-to-video model capability is unverified')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        guided_characters._enabled(db, pid)
        shots = db.execute('SELECT duration FROM shots WHERE project_id=?', (pid,)).fetchall()
        if any(s['duration'] not in cap['durations'] for s in shots):
            raise ValueError('Existing shot durations are incompatible with this video model')
        db.execute('INSERT INTO guided_video_choices VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET provider=excluded.provider,model=excluded.model', (pid,provider,model))
        guided_characters._changed(db, pid)
    return {'provider':provider, 'model':model, 'revision':revision+1}


def attempt(pid: str, attempt_id: str) -> dict:
    guided_compat.reconcile_outputs(pid)
    with core.connect() as db:
        guided_workflow._row(db, pid)
        return guided_compat.find(pid, attempt_id, db=db)


def abandon_pending(pid: str, attempt_id: str, *, confirm_checked: bool) -> dict:
    if confirm_checked is not True:
        raise ValueError('Explicit confirmation is required')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _job_for_action(db, pid, attempt_id)
        if job:
            _require_idle(job)
            current = jobs.latest_attempt(job['id'], db=db)
            if current and current['status'] == 'ABANDONED':
                return guided_compat.project_job(job, db=db)
            if not current or current['status'] not in ('UNKNOWN_REMOTE_STATE', 'DOWNLOADING', 'VALIDATING'):
                raise ValueError('Only an unknown or pending download attempt can be ended')
            attempt_store.abandon(current['id'], 'User checked; remote may continue and bill', db=db)
            db.execute("UPDATE jobs SET status='CANCELLED',cancel_requested=1,lease_owner=NULL,"
                       "lease_expires=NULL,updated=? WHERE id=?", (core.now(), job['id']))
            jobs.append_event(job['id'], 'guided_abandoned',
                              {'warning': 'remote may continue and bill'}, attempt_id=current['id'], db=db)
            return guided_compat.project_job(jobs.get(job['id'], db=db), db=db)
        row = db.execute('SELECT status,video_url FROM guided_attempts WHERE project_id=? AND id=?',
                         (pid, attempt_id)).fetchone()
        if row is None:
            raise ValueError('Unknown attempt')
        if row['status'] == 'ABANDONED':
            pass
        elif row['status'] == 'SUBMITTING' or (
                row['status'] == 'DOWNLOAD_PENDING' and row['video_url']):
            db.execute("UPDATE guided_attempts SET status='ABANDONED' WHERE project_id=? AND id=?",
                       (pid, attempt_id))
        else:
            raise ValueError('Only an unknown or completed remote result awaiting download can be ended')
    return attempt(pid, attempt_id)


def trim_pending(pid: str, attempt_id: str, *, confirm_checked: bool = False) -> dict:
    if not re.fullmatch(r'[0-9a-f]{32}', attempt_id):
        raise ValueError('Invalid attempt id')
    with core.connect() as db:
        timeline.migrate(db)
        db.execute('BEGIN IMMEDIATE')
        job = _job_for_action(db, pid, attempt_id)
        if job:
            _require_confirmation(confirm_checked)
            return _trim_job(db, job)
        row = db.execute('''SELECT a.*, s.seq, s.duration AS current_duration, s.archived
                            FROM guided_attempts a JOIN shots s ON s.id=a.shot_id AND s.project_id=a.project_id
                            WHERE a.project_id=? AND a.id=?''', (pid, attempt_id)).fetchone()
        if not row or row['status'] != 'DOWNLOAD_PENDING' or row['archived']:
            raise ValueError('Only an active shot with a pending downloaded result can be trimmed')
        directory = (core.HOME / pid / 'guided-attempts' / attempt_id).resolve()
        if not directory.is_relative_to((core.HOME / pid / 'guided-attempts').resolve()):
            raise ValueError('Invalid attempt directory')
        sources = sorted(p for p in directory.glob('*.mp4') if re.fullmatch(r'[0-9a-f]{32}\.mp4', p.name)
                         and p.is_file() and not p.is_symlink())
        if not sources:
            raise ValueError('No downloaded video found; recover the task first')
        source = sources[0]
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)],
                               capture_output=True, text=True, check=True)
        try:
            source_duration = float(probe.stdout.strip())
        except ValueError:
            raise ValueError('Cannot determine downloaded video duration') from None
        duration = row['current_duration']
        if not math.isfinite(source_duration) or source_duration < duration:
            raise ValueError('Downloaded video is shorter than the current shot; cannot trim')
        target = directory / ('trimmed-' + uuid4().hex + '.mp4')
        core.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n', '-i', str(source), '-t', str(duration),
                  '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                  '-c:a', 'aac', str(target)])
        core.verify_video(target, duration)
        shot = db.execute('SELECT * FROM shots WHERE project_id=? AND id=?', (pid, row['shot_id'])).fetchone()
        asset_id = timeline._register(db, pid, shot, 'guided:' + attempt_id, target)
        db.execute("UPDATE guided_attempts SET status='COMPLETED',file=?,asset_id=?,error=NULL WHERE project_id=? AND id=?",
                   (str(target), asset_id, pid, attempt_id))
    return attempt(pid, attempt_id)


def _require_confirmation(confirmed):
    if confirmed is not True:
        raise ValueError('Explicit confirmation is required: send confirm_checked=true')


def _job_for_action(db, pid, attempt_id):
    guided_workflow._row(db, pid)
    if db.execute('SELECT 1 FROM guided_attempts WHERE project_id=? AND id=?',
                   (pid, attempt_id)).fetchone():
        return None
    job = guided_compat.find_job(pid, attempt_id, db=db)
    if job is None:
        raise ValueError('Unknown attempt')
    latest = jobs.latest_attempt(job['id'], db=db)
    if attempt_id != job['id'] and (not latest or latest['id'] != attempt_id):
        raise ValueError('Historical attempt cannot change the latest job')
    return job


def _require_idle(job):
    if (job['status'] == 'RUNNING' and job['lease_owner'] and job['lease_expires']
            and datetime.fromisoformat(job['lease_expires']).replace(tzinfo=timezone.utc)
            > datetime.now(timezone.utc)):
        raise ValueError('An active worker owns this job; query it later')


def _trim_job(db, job):
    _require_idle(job)
    state = guided_compat.project_job(job, db=db)
    current = jobs.latest_attempt(job['id'], db=db)
    shot = db.execute('SELECT * FROM shots WHERE project_id=? AND id=? AND archived=0',
                      (job['project_id'], state['shot_id'])).fetchone()
    if not shot:
        raise ValueError('Only an active shot can be trimmed')
    output = json.loads(job['output_json'] or '{}')
    if state['status'] == 'DOWNLOAD_PENDING' and current and current['status'] in ('DOWNLOADING', 'VALIDATING'):
        event = db.execute(
            "SELECT data_json FROM job_events WHERE job_id=? AND type='download_ready' "
            'ORDER BY created DESC,rowid DESC LIMIT 1', (job['id'],)).fetchone()
        if event:
            output.update(json.loads(event['data_json']))
    if state['status'] not in ('COMPLETED', 'DOWNLOAD_PENDING') or not output.get('file'):
        raise ValueError('Only a completed or pending downloaded Job can be trimmed')
    if output.get('guided_trim_duration') == shot['duration']:
        return state
    source = Path(output['file']).resolve()
    if not source.is_relative_to((core.HOME / job['project_id']).resolve()) or not source.is_file():
        raise ValueError('Invalid completed video file')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=duration', '-of', 'default=nw=1:nk=1', str(source)],
                           capture_output=True, text=True, check=True)
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        raise ValueError('Cannot determine downloaded video duration') from None
    if not math.isfinite(duration) or duration < shot['duration']:
        raise ValueError('Downloaded video is shorter than the current shot; cannot trim')
    # Keep the original registered as a selectable version too.
    timeline._register(db, job['project_id'], shot, 'job:' + job['id'], source)
    target = source.with_name('trimmed-' + uuid4().hex + '.mp4')
    core.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n', '-i', str(source),
              '-t', str(shot['duration']), '-map', '0:v:0', '-map', '0:a:0?',
              '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(target)])
    core.verify_video(target, shot['duration'])
    asset = timeline._register(db, job['project_id'], shot, 'job:' + job['id'], target)
    output.update(file=str(target), asset_id=asset, sha256=timeline._hash(target),
                  original_file=output.get('original_file', str(source)),
                  guided_trim_duration=shot['duration'])
    jobs.append_event(job['id'], 'guided_trimmed', {'file': str(target), 'asset_id': asset,
                                                  'duration': shot['duration']}, db=db)
    if current and current['status'] in ('DOWNLOADING', 'VALIDATING'):
        attempt_store.mark_completed(current['id'], output, db=db)
    if job['status'] == 'BLOCKED':
        jobs.set_status(job['id'], 'SUCCEEDED', output=output, db=db)
    else:
        db.execute('UPDATE jobs SET output_json=?,updated=? WHERE id=?',
                   (json.dumps(output), core.now(), job['id']))
    return guided_compat.project_job(jobs.get(job['id'], db=db), db=db)


def _mock_frame_video(shot, target, first_frame, first_mime, last_frame, last_mime):
    suffixes = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
    first_suffix = suffixes.get(first_mime)
    last_suffix = suffixes.get(last_mime)
    if not first_suffix:
        raise ValueError('Unsupported first-frame type')
    if not last_suffix:
        raise ValueError('Unsupported last-frame type')
    first_source = target.with_name('first-frame' + first_suffix)
    last_source = target.with_name('last-frame' + last_suffix)
    with first_source.open('xb') as f:
        f.write(first_frame)
    with last_source.open('xb') as f:
        f.write(last_frame)
    duration = shot['duration']
    first_half = duration // 2
    last_half = duration - first_half
    parts = []
    concat = target.with_name('concat.txt')
    try:
        for name, source, half in (('first', first_source, first_half), ('last', last_source, last_half)):
            part = target.with_name(f'{name}-half.mp4')
            core.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-loop','1','-i',str(source),
                      '-vf',f'fps=24,scale=1280:720:force_original_aspect_ratio=decrease,'
                            f'pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=duration={half},setpts=N/(24*TB)',
                      '-frames:v',str(half*24),'-an','-c:v','libx264','-preset','veryfast',
                      '-pix_fmt','yuv420p',str(part)])
            parts.append(part)
        concat.write_text(''.join(f"file '{p.resolve()}'\n" for p in parts), encoding='utf-8')
        core.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0',
                  '-i',str(concat),'-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p',str(target)])
        core.verify_video(target,duration)
    finally:
        for path in (first_source, last_source, concat, *parts):
            path.unlink(missing_ok=True)


def _existing_job(db, pid, shot_id, request_id):
    row = _request_job(db, pid, request_id)
    if row is not None:
        if json.loads(row['payload_json']).get('shot_id') != shot_id:
            raise ValueError('Request id belongs to another shot')
        _remember_alias(db, row, request_id)
        return guided_compat.project_job(dict(row), db=db)
    row = db.execute(
        "SELECT * FROM jobs WHERE project_id=? AND target_key=? "
        "AND status IN ('PENDING','RUNNING','PAUSED','BLOCKED') ORDER BY created DESC LIMIT 1",
        (pid, f'shot:{shot_id}')).fetchone()
    if row is not None:
        if 'guided_request_id' not in json.loads(row['payload_json']):
            raise ValueError('An earlier non-Guided job is unresolved')
        _remember_alias(db, row, request_id)
        return guided_compat.project_job(dict(row), db=db)


def _request_job(db, pid, request_id):
    row = db.execute('SELECT * FROM jobs WHERE project_id=? AND request_key=?',
                     (pid, 'guided:' + request_id)).fetchone()
    if row is not None:
        return row
    return db.execute(
        "SELECT j.* FROM jobs j JOIN job_events e ON e.job_id=j.id "
        "WHERE j.project_id=? AND e.type='guided_request_alias' "
        "AND json_extract(e.data_json,'$.request_id')=? "
        'ORDER BY j.created DESC LIMIT 1', (pid, request_id)).fetchone()


def _remember_alias(db, job, request_id):
    if request_id == json.loads(job['payload_json']).get('guided_request_id'):
        return
    exists = db.execute(
        "SELECT 1 FROM job_events WHERE job_id=? AND type='guided_request_alias' "
        "AND json_extract(data_json,'$.request_id')=?", (job['id'], request_id)).fetchone()
    if not exists:
        jobs.append_event(job['id'], 'guided_request_alias', {'request_id': request_id}, db=db)


def submit(pid: str, seq: int, budget_usd: float, *, confirm_paid: bool, request_id: str) -> dict:
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    with core.connect() as db:
        shot = _shot(db, pid, seq)
        existing = _existing_job(db, pid, shot['id'], request_id)
        if existing:
            return existing
    estimate = preview(pid, seq)
    first_frame = last_frame = None
    first_mime = last_mime = None
    first_path = last_path = None
    if estimate['first_frame_id']:
        first_path = guided_images.image_path(pid, estimate['first_frame_id'])
        first_frame = first_path.read_bytes()
        if hashlib.sha256(first_frame).hexdigest() != estimate['first_frame_sha256']:
            raise ValueError('Confirmed boundary frame changed')
        first_mime = mimetypes.guess_type(first_path.name)[0]
        last_path = guided_images.image_path(pid, estimate['last_frame_id'])
        last_frame = last_path.read_bytes()
        if hashlib.sha256(last_frame).hexdigest() != estimate['last_frame_sha256']:
            raise ValueError('Confirmed boundary frame changed')
        last_mime = mimetypes.guess_type(last_path.name)[0]
    if estimate['paid'] and (confirm_paid is not True or isinstance(budget_usd, bool)
            or not isinstance(budget_usd, (int, float)) or not math.isfinite(budget_usd)
            or budget_usd <= 0 or budget_usd < estimate['estimated_cost_usd']):
        raise ValueError('Explicit confirmation and sufficient positive budget required')
    aid = uuid4().hex
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = dict(core.project(db, pid))
        shot = _shot(db, pid, seq)
        existing = db.execute('SELECT * FROM guided_attempts WHERE project_id=? AND request_id=?', (pid, request_id)).fetchone()
        if existing:
            if existing['shot_id'] != shot['id']:
                raise ValueError('Request id belongs to another shot')
            return dict(existing)
        existing = _existing_job(db, pid, shot['id'], request_id)
        if existing:
            return existing
        active = db.execute("SELECT 1 FROM guided_attempts WHERE project_id=? AND shot_id=? AND status IN ('SUBMITTING','SUBMITTED','RUNNING','DOWNLOAD_PENDING')", (pid, shot['id'])).fetchone()
        if active:
            raise ValueError('An earlier attempt is unresolved; recover it before submitting again')
        if shot['prompt'] != estimate['prompt'] or shot['duration'] != estimate['duration']:
            raise ValueError('Shot changed after preview')
        if estimate['first_frame_id']:
            start, end = guided_boundaries.for_shot(db, pid, shot['id'])
            choice = db.execute('SELECT provider,model FROM guided_video_choices WHERE project_id=?', (pid,)).fetchone()
            if (not start['confirmed_image_id'] or not end['confirmed_image_id']
                    or start['confirmed_image_id'] != estimate['first_frame_id']
                    or end['confirmed_image_id'] != estimate['last_frame_id']
                    or not choice or choice['provider'] != estimate['provider'] or choice['model'] != estimate['model']):
                raise ValueError('Boundary frames or video model changed after preview')
        reserved = db.execute('SELECT COALESCE(SUM(reserved_usd),0) FROM guided_attempts WHERE project_id=?', (pid,)).fetchone()[0]
        if estimate['paid'] and reserved + estimate['estimated_cost_usd'] > p['budget'] + 1e-9:
            raise ValueError('Project budget insufficient')
        if estimate['paid']:
            if ledger.committed(pid, db=db) + ledger.to_micro(estimate['estimated_cost_usd']) > ledger.to_micro(p['budget']):
                raise ValueError('Project budget insufficient')
            run_id = job_runner.open_or_reuse_run(
                pid, request_key=f'guided:{request_id}',
                budget_microusd=ledger.to_micro(budget_usd), db=db)
            job = job_runner.enqueue_shot(
                pid, seq, run_id=run_id, provider=estimate['provider'], model=estimate['model'],
                request_key=f'guided:{request_id}', db=db, guided_input={
                    'guided_request_id': request_id,
                    'first_frame_path': str(first_path) if first_path else None,
                    'first_mime': first_mime,
                    'last_frame_path': str(last_path) if last_path else None,
                    'last_mime': last_mime,
                    **{key: estimate[key] for key in ('first_frame_id', 'first_frame_sha256',
                                                      'last_frame_id', 'last_frame_sha256')},
                })
            return guided_compat.project_job(job, db=db)
        db.execute('''INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status)
                      VALUES (?,?,?,?,?,?,?,?,?)''', (aid, pid, shot['id'], request_id, shot['prompt'], shot['duration'],
                                                    estimate['provider'], estimate['estimated_cost_usd'], 'SUBMITTING'))
        if estimate['first_frame_id']:
            db.execute('INSERT INTO guided_video_provenance(attempt_id,frame_id,frame_sha256,last_frame_id,last_frame_sha256,model) VALUES (?,?,?,?,?,?)',
                       (aid, estimate['first_frame_id'], estimate['first_frame_sha256'],
                        estimate['last_frame_id'], estimate['last_frame_sha256'], estimate['model']))
    if estimate['provider'] == 'mock':
        try:
            directory = core.HOME / pid / 'guided-attempts' / aid
            directory.mkdir(parents=True, exist_ok=False)
            target = directory / 'video.mp4'
            if first_frame is not None:
                _mock_frame_video(shot, target, first_frame, first_mime, last_frame, last_mime)
            else:
                core.mock_generate(shot, target)
            core.verify_video(target, shot['duration'])
            _register(pid, aid, seq, target)
        except Exception as exc:
            _update(pid, aid, status='FAILED', error=str(exc)[:500])
        return attempt(pid, aid)


def _update(pid, aid, **values):
    allowed = {'status', 'remote_id', 'video_url', 'file', 'asset_id', 'error'}
    if not values or not values.keys() <= allowed:
        raise ValueError('Invalid attempt update')
    with core.connect() as db:
        db.execute('UPDATE guided_attempts SET ' + ','.join(k+'=?' for k in values) + " WHERE project_id=? AND id=? AND status NOT IN ('COMPLETED','ABANDONED')",
                   (*values.values(), pid, aid))


def _register(pid, aid, seq, target):
    asset = timeline.import_asset(pid, seq, str(target), source='guided:' + aid)
    _update(pid, aid, status='COMPLETED', file=str(target), asset_id=asset, error=None)
    # The system generated and registered a selectable asset for this shot. This
    # is an honest system decision, never a fabricated human confirmation.
    from . import storyboard_revisions
    with core.connect() as db:
        shot = db.execute('SELECT id FROM shots WHERE project_id=? AND seq=?',
                          (pid, seq)).fetchone()
        if shot:
            storyboard_revisions._record_decision(
                db, pid, node_type='shot', node_id=shot['id'], action='select',
                actor='system', metadata={'asset_id': asset})


def recover(pid: str, attempt_id: str, remote_id: str | None = None, *,
            confirm_checked: bool = False) -> dict:
    """Attach a user-verified ID or poll an existing task once; never submit."""
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _job_for_action(db, pid, attempt_id)
        if job:
            _require_confirmation(confirm_checked)
            _require_idle(job)
            current = jobs.latest_attempt(job['id'], db=db)
            if job['status'] in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
                return guided_compat.project_job(job, db=db)
            if job['status'] == 'PAUSED' or job['pause_requested'] or job['cancel_requested']:
                raise ValueError('Resume the paused/cancelled job before recovery')
            if not current:
                raise ValueError('Remote task ID requires an existing submitted attempt')
            if remote_id is not None:
                if not isinstance(remote_id, str) or not remote_id.strip():
                    raise ValueError('Invalid remote id')
                if current['remote_id']:
                    if remote_id.strip() != current['remote_id']:
                        raise ValueError('A different remote id is already attached')
                else:
                    current = attempt_store.attach_remote(current['id'], remote_id, db=db)
            if not current['remote_id']:
                raise ValueError('Remote task ID required; check provider dashboard before attaching')
            if jobs.recovery_action(job['id'], db=db) != 'resume':
                raise ValueError('Attempt cannot resume query/download')
            owner = 'guided-recover:' + uuid4().hex
            now, expiry = jobs._future(core.now(), 60)
            db.execute("UPDATE jobs SET status='RUNNING',lease_owner=?,lease_expires=?,"
                       "block_reason=NULL,error_class=NULL,error_detail=NULL,updated=? WHERE id=?",
                       (owner, expiry, now, job['id']))
            jobs.append_event(job['id'], 'guided_recover',
                              {'remote_id': current['remote_id']}, attempt_id=current['id'], db=db)
            job = jobs.get(job['id'], db=db)
    if job:
        _resume_job_once(job, current, owner)
        return attempt(pid, attempt_id)
    state = attempt(pid, attempt_id)
    if state['status'] not in _ACTIVE:
        return state
    if remote_id is not None:
        if not isinstance(remote_id, str) or not remote_id.strip() or state['remote_id']:
            raise ValueError('Invalid or already-attached remote id')
        _update(pid, attempt_id, remote_id=remote_id.strip(), status='SUBMITTED')
        state = attempt(pid, attempt_id)
    if not state['remote_id']:
        raise ValueError('Remote task ID required; check provider dashboard before attaching')
    try:
        with httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False) as client:
            provider = core.PROVIDERS[state['provider']](client)
            url = state['video_url']
            if not url:
                if state['provider'] == 'flux':
                    with core.connect() as db:
                        provenance = db.execute('SELECT model FROM guided_video_provenance WHERE attempt_id=?', (attempt_id,)).fetchone()
                    result = provider.poll(state['remote_id'], model=provenance['model'] if provenance else None)
                else:
                    result = provider.poll(state['remote_id'])
                if result.status == 'failed':
                    _update(pid, attempt_id, status='FAILED', error=result.error)
                    return attempt(pid, attempt_id)
                if result.status != 'succeeded':
                    _update(pid, attempt_id, status='RUNNING')
                    return attempt(pid, attempt_id)
                url = result.url
                _update(pid, attempt_id, status='DOWNLOAD_PENDING', video_url=url)
            from video_provider_core.media import download
            directory = core.HOME / pid / 'guided-attempts' / attempt_id
            directory.mkdir(parents=True, exist_ok=True)
            # A unique destination per download avoids overwrites even if two
            # recovery requests race. Existing files are never reused or removed.
            target = directory / (uuid4().hex + '.mp4')
            download(client, url, target)
            core.verify_video(target, state['duration'])
            with core.connect() as db:
                shot = db.execute('SELECT seq FROM shots WHERE project_id=? AND id=?', (pid, state['shot_id'])).fetchone()
            _register(pid, attempt_id, shot['seq'], target)
    except Exception as exc:
        _update(pid, attempt_id, error=str(exc)[:500])
    return attempt(pid, attempt_id)


def _resume_job_once(job, current, owner):
    """Lease-fenced single recovery poll, with the worker's heartbeat semantics."""
    from .handlers import video
    stopped, lost = threading.Event(), threading.Event()

    def beat():
        while not stopped.wait(15):
            if not jobs.heartbeat(job['id'], owner):
                lost.set()
                return

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        output = video.resume(job, current, worker.Control(lost), poll_once=True)
        if lost.is_set():
            raise jobs.LeaseLost('Worker no longer owns the job')
        jobs.set_status(job['id'], 'SUCCEEDED' if output is not None else 'PENDING',
                        output=output, owner=owner)
    except jobs.LeaseLost:
        jobs.append_event(job['id'], 'lease_lost', {'owner': owner})
    except worker.JobBlocked as exc:
        jobs.set_status(job['id'], 'BLOCKED', block_reason=exc.reason, owner=owner)
    except worker.JobStopped as exc:
        jobs.set_status(job['id'], 'PAUSED' if exc.action == 'pause' else 'CANCELLED', owner=owner)
    except Exception as exc:
        latest = jobs.latest_attempt(job['id'])
        if latest['status'] in ('FAILED_FINAL', 'FAILED_RETRYABLE'):
            jobs.set_status(job['id'], 'FAILED', error_detail=str(exc)[:500], owner=owner)
        else:
            if latest['status'] in attempt_store.ACTIVE:
                attempt_store.mark_unknown(latest['id'], str(exc))
            jobs.set_status(job['id'], 'BLOCKED', block_reason='unknown_remote',
                            error_detail=str(exc)[:500], owner=owner)
    finally:
        stopped.set()
        thread.join(timeout=2)
        jobs.release(job['id'], owner)
