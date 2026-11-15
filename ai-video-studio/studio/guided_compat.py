"""Read-only projection of persistent Guided video jobs into legacy attempts."""
import json

from . import job_runner, jobs


def project_job(job: dict, *, db=None) -> dict:
    payload = json.loads(job['payload_json'])
    with job_runner._conn(db) as conn:
        attempt = jobs.latest_attempt(job['id'], db=conn)
    status = {'PENDING': 'QUEUED', 'RUNNING': 'SUBMITTING',
              'SUCCEEDED': 'COMPLETED', 'BLOCKED': 'SUBMITTING',
              'FAILED': 'FAILED', 'CANCELLED': 'ABANDONED'}.get(job['status'])
    if status is None:
        raise ValueError(f"Unsupported Guided job status: {job['status']}")
    return {
        'id': attempt['id'] if attempt else job['id'],
        'project_id': job['project_id'], 'shot_id': payload['shot_id'],
        'request_id': payload['guided_request_id'], 'prompt': payload['prompt'],
        'duration': payload['duration_sec'], 'provider': payload['provider'],
        'reserved_usd': 0, 'status': status,
        'remote_id': attempt['remote_id'] if attempt else None,
        'video_url': None, 'file': None, 'asset_id': None,
        'error': job['error_detail'] or job['block_reason'],
        'created': job['created'],
    }
