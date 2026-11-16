"""Guided storyboard planning job: reserve the user ceiling, call the LLM once,
and persist the validated draft atomically without overwriting a stale revision.
"""
from __future__ import annotations

import json

from .. import jobs, ledger
from .. import guided_storyboard, guided_workflow


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
        draft = guided_storyboard.generate_draft(payload)
        guided_workflow.save_draft(pid, draft, payload['revision'])
    except guided_storyboard.UnknownRemote as exc:
        # The request may have been sent; retain the reservation and block.
        attempts.mark_unknown(attempt['id'], str(exc))
        jobs.append_event(job['id'], 'storyboard_unknown', {'error': str(exc)[:500]},
                          attempt_id=attempt['id'])
        control.block('unknown_remote')
    except Exception as exc:
        # A known/validation failure (or stale revision) is permanent: the
        # request may already be billed, so the reservation is retained and
        # this must never fall through to the worker's unknown_remote path.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    attempts.mark_completed(attempt['id'], {'result': draft})
    return {'result': draft}
