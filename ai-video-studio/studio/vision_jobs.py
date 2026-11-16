"""Enqueue and execute paid visual-review Jobs (Director per-shot reviews).

One ``director-review`` command opens a single shared Run and one
``vision_review`` Job per selected shot. A stable batch key (an explicit
``request_id`` or a content hash) makes a repeated identical live command
idempotent: the same Run and Jobs are reused, and no second paid request is ever
opened for the same work.

The exception classes below are the single set the ``vision_review`` handler
catches. ``director_vision``/``continuity_vision`` executors raise these rather
than defining their own.
"""
from __future__ import annotations

import hashlib
import json
import os

from . import ledger, request_jobs


class UnknownRemote(Exception):
    """The provider request may have been sent but its outcome is unknown."""


class AuthFailure(Exception):
    """The provider rejected the request as unauthorized (401/403)."""


class RateLimited(Exception):
    """The provider rejected the request as rate limited (429)."""


class PreRequestFailure(Exception):
    """A deterministic failure proven to occur before any provider request."""


class PostResponseFailure(Exception):
    """The provider returned a response but its content was rejected."""


def _batch_key(kind, report_id, model, selections, ceilings) -> str:
    """Stable 32-hex content identity for a vision review batch."""
    canonical = json.dumps(
        {'kind': kind, 'report_id': report_id, 'model': model,
         'selections': sorted(selections) if selections is not None else None,
         'ceilings': list(ceilings)},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


def enqueue_director(pid: str, report_id: str, model: str, base_url,
                     budget_usd: float, per_shot_ceiling_usd: float,
                     selected, request_id: str | None = None) -> dict:
    """Validate the existing plan and enqueue one ``vision_review`` Job per shot.

    Refuses to create any Job when the total reserved ceiling exceeds the budget.
    The batch shares one Run; the ``review_id`` is a stable 12-hex digest of the
    request id, so a replay writes the same review directory.
    """
    from . import director_vision
    _, shots, estimate = director_vision.plan(
        pid, report_id, per_shot_ceiling_usd, budget_usd, selected)
    if not estimate['within_budget']:
        raise ValueError('Budget insufficient: no requests made')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Model is required')
    if not shots:
        raise ValueError('No shots to review')
    if request_id is None:
        request_id = _batch_key(
            'director', report_id, model, selected,
            (budget_usd, per_shot_ceiling_usd))
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    review_id = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:12]
    endpoint = (base_url or os.environ.get('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1').rstrip('/')
    per_shot_micro = ledger.to_micro(per_shot_ceiling_usd)
    # The Run's budget scope must match the idempotency identity (the request
    # id), not the content hash, so a distinct explicit request id over the
    # same content opens its own Run instead of silently sharing the first.
    run_key = f'vision:{request_id}'
    created = []
    for shot in shots:
        payload = {
            'kind': 'director',
            'project_id': pid,
            'report_id': report_id,
            'review_id': review_id,
            'seq': shot['seq'],
            'prompt': shot['prompt'],
            'frames': [f['file'] for f in shot['frames']],
            'source_asset_id': shot['asset_id'],
            'model': model,
            'base_url': endpoint,
            'budget_microusd': per_shot_micro,
        }
        job = request_jobs.submit(
            pid, kind='vision_review', request_id=f'{review_id}:{shot["seq"]}',
            payload=payload, budget_usd=budget_usd,
            target_key=f'director:{report_id}:{shot["seq"]}', run_key=run_key)
        created.append(job)
    return {'review_id': review_id, 'run_id': created[0]['run_id'],
            'job_ids': [j['id'] for j in created], 'request_id': request_id,
            'estimate': estimate}


def enqueue_continuity(pid: str, report_id: str, model: str, base_url,
                       budget_usd: float, per_pair_ceiling_usd: float,
                       pairs, request_id: str | None = None) -> dict:
    """Validate the continuity report and enqueue one ``vision_review`` Job per
    ready boundary pair.

    Refuses to create any Job when the total reserved ceiling exceeds the budget.
    The batch shares one Run; the ``review_id`` is a stable 12-hex digest of the
    request id, so a replay writes the same review directory.
    """
    from . import continuity_vision
    _, targets = continuity_vision._targets(pid, report_id, pairs)
    reserve = round(len(targets) * per_pair_ceiling_usd, 6)
    estimate = {'pairs': len(targets),
                'per_pair_ceiling_usd': per_pair_ceiling_usd,
                'reserved_ceiling_usd': reserve, 'budget_usd': budget_usd,
                'within_budget': reserve <= budget_usd}
    if not estimate['within_budget']:
        raise ValueError('Budget insufficient: no requests made')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Model is required')
    if not targets:
        raise ValueError('No pairs to review')
    if request_id is None:
        request_id = _batch_key(
            'continuity', report_id, model, pairs,
            (budget_usd, per_pair_ceiling_usd))
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    review_id = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:12]
    endpoint = (base_url or os.environ.get('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1').rstrip('/')
    per_pair_micro = ledger.to_micro(per_pair_ceiling_usd)
    run_key = f'continuity:{request_id}'
    created = []
    for b in targets:
        payload = {
            'kind': 'continuity',
            'project_id': pid,
            'report_id': report_id,
            'review_id': review_id,
            'from_seq': b['from_seq'],
            'to_seq': b['to_seq'],
            'from_prompt': b['from_prompt'],
            'to_prompt': b['to_prompt'],
            'frames': [f['file'] for f in b['frames']['out'] + b['frames']['in']],
            'source_asset_ids': [b['from_asset_id'], b['to_asset_id']],
            'model': model,
            'base_url': endpoint,
            'budget_microusd': per_pair_micro,
        }
        job = request_jobs.submit(
            pid, kind='vision_review', request_id=f'{review_id}:{b["from_seq"]}',
            payload=payload, budget_usd=budget_usd,
            target_key=f'continuity:{report_id}:{b["from_seq"]}', run_key=run_key)
        created.append(job)
    return {'review_id': review_id, 'run_id': created[0]['run_id'],
            'job_ids': [j['id'] for j in created], 'request_id': request_id,
            'estimate': estimate}
