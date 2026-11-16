"""Enqueue and execute the paid narration planning Job.

One request maps to one ``llm_narration`` Job whose immutable payload carries the
project prompt, language, model, base URL, an allowed-shots snapshot
(seq/duration/prompt) and that snapshot's digest. The LLM call and line
validation happen in ``narration.generate_script``; the snapshot re-check and
atomic ``script.json`` write happen here, never in the enqueue path.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

from . import core, request_jobs


class StaleEvidence(ValueError):
    """The storyboard snapshot changed after the request was enqueued."""


def _shots_digest(snapshot: list[dict]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def enqueue_plan(pid: str, *, language: str, model: str, base_url: str | None,
                 budget_usd: float, request_id: str) -> dict:
    """Validate show-independent plan preconditions and enqueue one Job."""
    core.require_paid_budget(budget_usd)
    if not isinstance(model, str) or not model.strip():
        raise ValueError('LLM model is required')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    with core.connect() as db:
        project = dict(core.project(db, pid))
        rows = db.execute(
            'SELECT seq,prompt,duration FROM shots WHERE project_id=? ORDER BY seq',
            (pid,))
        shots = [dict(r) for r in rows]
    if not shots:
        raise ValueError('Project has no storyboard')
    if base_url is None:
        base_url = os.getenv('STUDIO_LLM_BASE_URL', 'https://api.openai.com/v1')
    snapshot = [{'seq': s['seq'], 'duration': s['duration'], 'prompt': s['prompt']}
                for s in shots]
    payload = {
        'project_id': pid,
        'prompt': project['prompt'],
        'language': language,
        'model': model,
        'base_url': base_url,
        'shots': snapshot,
        'source_digest': _shots_digest(snapshot),
        'budget_usd': budget_usd,
        'request_id': request_id,
    }
    job = request_jobs.submit(pid, kind='llm_narration', request_id=request_id,
                              payload=payload, budget_usd=budget_usd,
                              target_key=f'narration:{pid}')
    return {'status': 'QUEUED', 'job_id': job['id'], 'request_id': request_id}


def save_script(pid: str, script: dict, payload: dict) -> str:
    """Write script.json atomically only if the shots still match the snapshot."""
    with core.connect() as db:
        core.project(db, pid)
        rows = db.execute(
            'SELECT seq,prompt,duration FROM shots WHERE project_id=? ORDER BY seq',
            (pid,))
        snapshot = [{'seq': r['seq'], 'duration': r['duration'], 'prompt': r['prompt']}
                    for r in rows]
    if _shots_digest(snapshot) != payload['source_digest']:
        raise StaleEvidence('Storyboard changed during narration generation')
    from . import narration
    target = narration._script_path(pid)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix='.script-',
                               suffix='.json')
    os.close(fd)
    try:
        os.chmod(tmp, 0o644)
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(script, ensure_ascii=False, indent=2))
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return str(target)
