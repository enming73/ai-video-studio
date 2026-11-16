"""Guided storyboard planning job: reserve the user ceiling, call the LLM once,
and persist the validated draft atomically without overwriting a stale revision.
"""
from __future__ import annotations

import json

from .. import ledger
from .. import guided_storyboard, guided_workflow


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    pid = job['project_id']
    attempt = attempts.create(job['id'], provider='llm', model=payload['model'],
                              input_data=payload)
    attempts.prepare_submission(
        attempt['id'], amount_microusd=ledger.to_micro(payload['budget_usd']),
        pricing_version=f'user_ceiling:{job["request_key"]}')
    control.checkpoint(job['id'])
    draft = guided_storyboard.generate_draft(payload)
    try:
        guided_workflow.save_draft(pid, draft, payload['revision'])
    except Exception as exc:
        # A stale revision must not write the draft; fail permanently, no retry.
        attempts.mark_failed(attempt['id'], 'FAILED_FINAL', str(exc))
        raise
    attempts.mark_completed(attempt['id'], {'result': draft})
    return {'result': draft}
