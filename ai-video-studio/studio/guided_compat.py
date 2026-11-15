"""Read-only projection of persistent Guided video jobs into legacy attempts."""
import json
from pathlib import Path

from . import core, guided_workflow, job_runner, jobs, ledger, timeline


_ATTEMPT_STATUS = {
    'PLANNED': 'QUEUED', 'RESERVED': 'QUEUED', 'SUBMITTING': 'SUBMITTING',
    'SUBMITTED': 'SUBMITTED', 'POLLING': 'SUBMITTED',
    'DOWNLOADING': 'DOWNLOAD_PENDING', 'VALIDATING': 'DOWNLOAD_PENDING',
    'COMPLETED': 'COMPLETED', 'UNKNOWN_REMOTE_STATE': 'SUBMITTING',
    'FAILED_RETRYABLE': 'FAILED', 'FAILED_FINAL': 'FAILED', 'ABANDONED': 'ABANDONED',
}


def project_job(job: dict, *, db=None) -> dict:
    payload = json.loads(job['payload_json'])
    required = {'shot_id', 'guided_request_id', 'prompt', 'duration_sec', 'provider'}
    if job['kind'] != 'video' or not required <= payload.keys():
        raise ValueError('Unsupported Guided job payload')
    with job_runner._conn(db) as conn:
        attempt = jobs.latest_attempt(job['id'], db=conn)
        output = json.loads((attempt or {}).get('output_json') or '{}')
        output.update(json.loads(job['output_json'] or '{}'))
        amount = 0
        if attempt:
            for entry in conn.execute('SELECT * FROM cost_entries WHERE attempt_id=?',
                                       (attempt['id'],)):
                if entry['billing_status'] == 'settled':
                    amount += entry['actual_microusd']
                elif entry['billing_status'] in ('reserved', 'unknown'):
                    amount += entry['reserved_microusd']
        asset_id = output.get('asset_id')
        if not asset_id and output.get('file') and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shot_assets'").fetchone():
            asset = conn.execute(
                'SELECT id FROM shot_assets WHERE project_id=? AND shot_id=? AND file=? AND source=?',
                (job['project_id'], payload['shot_id'], output['file'], 'job:' + job['id'])).fetchone()
            asset_id = asset['id'] if asset else None
    status = {'PENDING': 'QUEUED', 'RUNNING': 'SUBMITTING',
              'SUCCEEDED': 'COMPLETED', 'BLOCKED': 'SUBMITTING',
              'FAILED': 'FAILED', 'CANCELLED': 'ABANDONED'}.get(job['status'])
    if status is None:
        raise ValueError(f"Unsupported Guided job status: {job['status']}")
    if attempt and job['status'] not in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
        status = _ATTEMPT_STATUS.get(attempt['status'])
        if status is None:
            raise ValueError(f"Unsupported Guided attempt status: {attempt['status']}")
    return {
        'id': attempt['id'] if attempt else job['id'],
        'project_id': job['project_id'], 'shot_id': payload['shot_id'],
        'request_id': payload['guided_request_id'], 'prompt': payload['prompt'],
        'duration': payload['duration_sec'], 'provider': payload['provider'],
        'reserved_usd': ledger.to_usd(amount), 'status': status,
        'remote_id': attempt['remote_id'] if attempt else None,
        'video_url': output.get('video_url'), 'file': output.get('file'), 'asset_id': asset_id,
        'error': (None if status == 'COMPLETED' else
                  job['error_detail'] or job['block_reason'] or (attempt or {}).get('error_detail')),
        'created': job['created'],
    }


def _guided_jobs(conn, pid):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM jobs WHERE project_id=? AND kind='video' "
        "AND json_type(payload_json,'$.guided_request_id')='text'", (pid,))]


def attempt_records(pid, db=None) -> list[dict]:
    with job_runner._conn(db) as conn:
        guided_workflow._row(conn, pid)
        # The legacy table has no timestamp. Keep its row order, behind dated jobs.
        records = [{**dict(row), 'created': ''} for row in conn.execute(
            'SELECT * FROM guided_attempts WHERE project_id=? ORDER BY rowid DESC', (pid,))]
        records.extend(project_job(job, db=conn) for job in _guided_jobs(conn, pid))
        unique = {}
        for row in records:
            unique.setdefault(row['id'], row)
        return sorted(unique.values(), key=lambda row: row['created'], reverse=True)


def find_job(pid, attempt_id, db=None) -> dict | None:
    with job_runner._conn(db) as conn:
        row = conn.execute(
            "SELECT j.* FROM jobs j WHERE j.project_id=? AND j.kind='video' "
            "AND json_type(j.payload_json,'$.guided_request_id')='text' "
            'AND (j.id=? OR EXISTS (SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.id=?))',
            (pid, attempt_id, attempt_id)).fetchone()
        return dict(row) if row else None


def find(pid, attempt_id, db=None) -> dict:
    with job_runner._conn(db) as conn:
        row = conn.execute('SELECT * FROM guided_attempts WHERE project_id=? AND id=?',
                           (pid, attempt_id)).fetchone()
        if row:
            return {**dict(row), 'created': ''}
        job = find_job(pid, attempt_id, db=conn)
        if job:
            return project_job(job, db=conn)
        raise ValueError('Unknown attempt')


def reconcile_outputs(pid):
    """Register completed outputs separately from the read-only projection."""
    with core.connect() as db:
        guided_workflow._row(db, pid)
        timeline.migrate(db)
        db.execute('BEGIN IMMEDIATE')
        for job in _guided_jobs(db, pid):
            if job['status'] != 'SUCCEEDED':
                continue
            payload = json.loads(job['payload_json'])
            output = json.loads(job['output_json'] or '{}')
            file = output.get('file')
            if not file or not Path(file).is_file():
                continue
            shot = db.execute('SELECT * FROM shots WHERE project_id=? AND id=?',
                              (pid, payload['shot_id'])).fetchone()
            if shot:
                timeline._register(db, pid, shot, 'job:' + job['id'], file)


def asset_provenance(db, pid, shot_id, asset_id):
    """Boundary snapshot for an asset registered from a Guided Job."""
    row = db.execute(
        "SELECT j.payload_json FROM shot_assets a JOIN jobs j ON a.source='job:' || j.id "
        "WHERE a.id=? AND a.project_id=? AND a.shot_id=? AND j.project_id=a.project_id "
        "AND json_extract(j.payload_json,'$.shot_id')=a.shot_id "
        "AND json_type(j.payload_json,'$.guided_request_id')='text'",
        (asset_id, pid, shot_id)).fetchone()
    if not row:
        return None
    payload = json.loads(row['payload_json'])
    return {'frame_id': payload.get('first_frame_id'),
            'frame_sha256': payload.get('first_frame_sha256'),
            'last_frame_id': payload.get('last_frame_id'),
            'last_frame_sha256': payload.get('last_frame_sha256'),
            'model': payload['model']}
