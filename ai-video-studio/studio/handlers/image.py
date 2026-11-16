"""Guided image generation job: reserve the user ceiling once, call the image
API, and persist the generated image atomically."""
from __future__ import annotations

import json

from .. import image_jobs, jobs, ledger


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    attempt = attempts.create(job['id'], provider='image', model=payload['model'],
                              input_data=payload)
    entry = attempts.prepare_submission(
        attempt['id'], amount_microusd=ledger.to_micro(payload['budget_usd']),
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    try:
        if payload['target'] == 'character':
            result = image_jobs.execute_character(payload)
        elif payload['target'] == 'boundary':
            result = image_jobs.execute_boundary(payload)
        else:
            raise image_jobs.PreRequestFailure('Unknown image target')
    except image_jobs.UnknownRemote as exc:
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'image_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except image_jobs.PreRequestFailure as exc:
        # Proven no provider call: the reservation was never billed, release it.
        ledger.release(entry, evidence=f'image failed before request: {str(exc)[:200]}')
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    except image_jobs.PostResponseFailure as exc:
        # The provider returned 200; retain the reservation for rejected content.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='content_rejected')
        raise
    attempts.mark_completed(attempt['id'], {'image_id': result['image_id']})
    return {'image_id': result['image_id']}
