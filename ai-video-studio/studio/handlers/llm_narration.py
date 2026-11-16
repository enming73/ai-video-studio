"""Narration planning job: reserve the user ceiling, call the LLM once, and
persist the validated script atomically without overwriting a stale storyboard."""
from __future__ import annotations

import json

from .. import jobs, ledger
from .. import guided_storyboard, narration, narration_jobs


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    pid = job['project_id']
    # Deterministic pre-request validation happens before any attempt or
    # reservation, so a missing key/insecure endpoint leaves no phantom cost.
    guided_storyboard.require_llm_config(payload.get('base_url'))
    attempt = attempts.create(job['id'], provider='llm', model=payload['model'],
                              input_data=payload)
    attempts.prepare_submission(
        attempt['id'], amount_microusd=ledger.to_micro(payload['budget_usd']),
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    try:
        script = narration.generate_script(payload)
        path = narration_jobs.save_script(pid, script, payload)
    except narration.UnknownRemote as exc:
        # The request may have been sent; retain the reservation and block.
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'narration_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except narration_jobs.StaleEvidence as exc:
        # The storyboard changed; the provider was called, so retain the
        # reservation and fail permanently without overwriting any script.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='stale_revision')
        jobs.append_event(job['id'], 'narration_stale', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        raise
    except Exception as exc:
        # A known/validation failure is permanent; the request may already be
        # billed, so the reservation is retained and this must never fall
        # through to the worker's unknown_remote path.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    attempts.mark_completed(attempt['id'], {'script': script})
    lines_data = script['lines']
    return {'script': path, 'lines': len(lines_data), 'lines_data': lines_data}
