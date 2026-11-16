"""Per-shot TTS segment job: reserve the ceiling, synthesize one line, write a
normalized WAV, and never touch the final narration output.

A missing key or insecure endpoint is deterministic and raised before any
request, so the reservation is released and the Job fails permanently. An
ambiguous provider outcome (timeout/HTTP/parse) retains the reservation and
blocks the Job for manual reconciliation.
"""
from __future__ import annotations

import json

from .. import jobs, ledger, narration


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    attempt = attempts.create(job['id'], provider='tts', model=payload['model'],
                              input_data=payload)
    entry = attempts.prepare_submission(
        attempt['id'], amount_microusd=payload['budget_microusd'],
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    try:
        result = narration.generate_segment(payload)
    except narration.ConfigError as exc:
        # Deterministic local-config failure before any request: release and fail.
        ledger.release(entry, evidence=f'tts not sent: {str(exc)[:200]}')
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='local_config')
        jobs.append_event(job['id'], 'tts_config', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        raise
    except narration.UnknownRemote as exc:
        # The request may have been sent; retain the reservation and block.
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'tts_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except Exception as exc:
        # A known post-request failure is permanent; retain the reservation.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    attempts.mark_completed(attempt['id'], {'file': result['file'],
                                            'duration': result['duration']})
    return {'file': result['file'], 'duration': result['duration']}
