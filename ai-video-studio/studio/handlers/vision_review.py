"""Per-shot vision review job: reserve the per-shot ceiling, call the vision
model once, and return the normalized review.

A frame/path/config failure is deterministic and raised before any request, so
the reservation is released and the Job fails permanently. An ambiguous provider
outcome (timeout/HTTP/parse) retains the reservation and blocks the Job for
manual reconciliation. A 200 whose content is rejected retains the reservation
and fails permanently.
"""
from __future__ import annotations

import json

from .. import director_vision, jobs, ledger, vision_jobs


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    attempt = attempts.create(job['id'], provider='vision', model=payload['model'],
                              input_data=payload)
    entry = attempts.prepare_submission(
        attempt['id'], amount_microusd=payload['budget_microusd'],
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    try:
        if payload['kind'] == 'director':
            result = director_vision.execute_shot(payload)
        else:
            raise vision_jobs.PreRequestFailure('Unknown vision review kind')
    except vision_jobs.UnknownRemote as exc:
        # The request may have been sent; retain the reservation and block.
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'vision_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except vision_jobs.PreRequestFailure as exc:
        # Proven no provider call: the reservation was never billed, release it.
        ledger.release(entry, evidence=f'vision not sent: {str(exc)[:200]}')
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='local_config')
        raise
    except vision_jobs.PostResponseFailure as exc:
        # The provider returned 200; retain the reservation for rejected content.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='content_rejected')
        raise
    attempts.mark_completed(attempt['id'], result)
    return result
