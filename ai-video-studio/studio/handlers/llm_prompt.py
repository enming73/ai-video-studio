"""Guided boundary-prompt job: reserve the user ceiling, call the LLM once, and
persist the validated prompt only if the boundary source snapshot is unchanged.
"""
from __future__ import annotations

import json

from .. import jobs, ledger
from .. import prompt_jobs


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    attempt = attempts.create(job['id'], provider='llm', model=payload['model'],
                              input_data=payload)
    entry = attempts.prepare_submission(
        attempt['id'], amount_microusd=ledger.to_micro(payload['budget_usd']),
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    try:
        result = prompt_jobs.execute(payload)
    except prompt_jobs.UnknownRemote as exc:
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'prompt_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except prompt_jobs.StaleEvidence as exc:
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc),
                             error_class='stale_revision')
        jobs.append_event(job['id'], 'prompt_stale', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        raise
    except Exception as exc:
        ledger.release(entry, evidence=f'prompt failed before submission: {str(exc)[:200]}')
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    attempts.mark_completed(attempt['id'], {'prompt': result['prompt']})
    return {'prompt': result['prompt']}
