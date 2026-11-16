"""Enqueue and execute paid guided boundary-prompt LLM jobs.

One request maps to one ``llm_prompt`` Job whose immutable payload carries the
boundary id, model, source snapshot, prompt payload, revision and budget. The
LLM call, length validation, snapshot re-check and domain-row writes all happen
in the executor, never in the enqueue path, and never in the legacy
``guided_boundary_prompt_attempts`` table.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from . import core, guided_boundaries, guided_compat, guided_workflow, request_jobs


class UnknownRemote(Exception):
    """The LLM request may have been sent but its outcome is unknown."""


class StaleEvidence(ValueError):
    """The boundary source snapshot changed after the request was enqueued."""


_UNKNOWN_ERRORS = (httpx.HTTPError, OSError, ValueError, KeyError, IndexError, TypeError)


def _reject_unresolved(projected: dict) -> dict:
    if projected['status'] == 'UNKNOWN':
        raise ValueError('Previous prompt submission is unresolved')
    return projected


def enqueue(pid: str, boundary_id: str, model: str, budget_usd: float, *,
            confirm_paid: bool, request_id: str, revision: int) -> dict:
    details = guided_boundaries.preview_prompt(pid, boundary_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    with core.connect() as db:
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
    payload = {
        'project_id': pid, 'boundary_id': boundary_id, 'model': details['model'],
        'budget_usd': budget_usd, 'request_id': request_id,
        'source_sha256': details['source_sha256'],
        'prompt_payload': details['payload'], 'revision': revision,
    }
    job = request_jobs.submit(pid, kind='llm_prompt', request_id=request_id, payload=payload,
                              budget_usd=budget_usd, target_key=f'prompt:{boundary_id}')
    return _reject_unresolved(guided_compat.project_prompt_job(job))


def execute(payload: dict) -> dict:
    """Call the LLM once and persist the validated prompt only if the boundary
    source snapshot is still current. No legacy rows are written."""
    pid = payload['project_id']
    boundary_id = payload['boundary_id']
    model = payload['model']
    load_dotenv()
    base_url = os.getenv('STUDIO_LLM_BASE_URL', 'https://api.openai.com/v1')
    if not os.getenv('STUDIO_LLM_API_KEY'):
        raise ValueError('Set STUDIO_LLM_API_KEY (no paid request made)')
    if not base_url.startswith('https://') and not os.getenv('STUDIO_ALLOW_HTTP_LLM'):
        raise ValueError('LLM endpoint must use HTTPS (or set STUDIO_ALLOW_HTTP_LLM=1 for local use)')
    try:
        result = core._llm_json(guided_boundaries._LLM_SYSTEM,
                                json.dumps(payload['prompt_payload'], ensure_ascii=False),
                                model, base_url)
    except _UNKNOWN_ERRORS as exc:
        raise UnknownRemote(str(exc)) from exc
    text = result.get('prompt') if isinstance(result, dict) else None
    if not isinstance(text, str) or not 20 <= len(text.strip()) <= 4000:
        raise UnknownRemote('Invalid prompt from the LLM')
    text = text.strip()
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._row(db, pid)
        if not db.execute('SELECT 1 FROM guided_boundaries WHERE id=? AND project_id=? AND active=1',
                          (boundary_id, pid)).fetchone():
            raise StaleEvidence('Boundary changed during prompt generation')
        _, _, digest = guided_boundaries._source_payload(db, pid, boundary_id)
        if digest != payload['source_sha256']:
            raise StaleEvidence('Boundary source changed during prompt generation')
        db.execute('INSERT INTO guided_boundary_prompts VALUES (?,?,?,?,?,?,?)',
                   (uuid4().hex, pid, boundary_id, text, payload['source_sha256'],
                    model, core.now()))
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    return {'prompt': text}
