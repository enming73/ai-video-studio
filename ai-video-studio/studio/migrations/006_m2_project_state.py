"""Initialize ``project_state`` rows for existing projects.

This migration is deliberately shallow: it only inserts a ``project_state``
row for every existing project, so legacy materialization can later attach
current revision pointers lazily. It must never probe media (FFprobe), create
revisions, or touch legacy tables. Legacy materialization stays lazy on first
read/write access (``edit_model.ensure_legacy_revisions``), so migration cannot
fail when FFprobe or media files are missing.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def apply(db) -> None:
    # ``projects`` is created by core.connect(), not by migrations; when this
    # migration runs in isolation it may not exist yet, so treat it as no-op.
    if not _table(db, 'projects'):
        return
    for row in db.execute('SELECT id FROM projects'):
        db.execute(
            'INSERT OR IGNORE INTO project_state(project_id, updated) VALUES (?, ?)',
            (row['id'], _now()),
        )
