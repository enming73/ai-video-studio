"""Local, offline render job: render a frozen snapshot manifest to disk.

No paid API calls and no network. ``execute`` renders the snapshot's frozen
video clips to a fresh output path (never overwriting) and marks the snapshot
``READY``; on failure it marks the snapshot ``FAILED`` and re-raises so the job
also fails.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .. import core, render_snapshots


def execute(job, attempts, control):
    control.checkpoint(job['id'])
    payload = json.loads(job['payload_json'])
    snapshot_id = payload['snapshot_id']
    pid = job['project_id']
    try:
        snapshot = render_snapshots.get(pid, snapshot_id)
        manifest = snapshot['manifest']
        output = core.HOME / pid / 'renders' / snapshot_id / f'{uuid4().hex}.mp4'
        render_snapshots._render_video(manifest, output)
        render_snapshots._mark_ready(pid, snapshot_id, str(output))
        render_snapshots.record_history(pid, snapshot_id, manifest, output)
        return {'snapshot_id': snapshot_id, 'output': str(output)}
    except Exception as exc:
        render_snapshots._mark_failed(pid, snapshot_id, str(exc)[:1200])
        raise


def resume(job, attempt, control):
    # Local rendering is deterministic and idempotent; re-execute from scratch.
    return execute(job, attempt, control)
