"""Import pre-M1 reservations once. Client authorization ceilings are not spend."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

MICRO_PER_USD = 1_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_micro(value) -> int:
    if isinstance(value, bool):
        raise ValueError('Money cannot be a boolean')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid legacy amount') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('Legacy amount must be finite and nonnegative')
    return int((amount * MICRO_PER_USD).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _table(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_guided(db) -> bool:
    return _table(db, 'guided_attempts')


def apply(db) -> None:
    sources: list[tuple[str, str, float, str]] = []
    if _has_guided(db):
        for row in db.execute(
                'SELECT id,project_id,reserved_usd FROM guided_attempts '
                'WHERE reserved_usd > 0'):
            sources.append(('guided_attempts:' + row['id'], row['project_id'],
                            row['reserved_usd'], 'guided_attempts'))
    if _table(db, 'shots'):
        sql = 'SELECT id,project_id,reserved_usd FROM shots WHERE reserved_usd > 0'
        if _has_guided(db):
            sql += (' AND NOT EXISTS (SELECT 1 FROM guided_attempts g '
                    'WHERE g.shot_id=shots.id)')
        for row in db.execute(sql):
            sources.append(('shots:' + row['id'], row['project_id'],
                            row['reserved_usd'], 'shots'))
    if not sources:
        return
    projects = sorted({source[1] for source in sources})
    for pid in projects:
        db.execute(
            'INSERT OR IGNORE INTO runs(id,project_id,status,budget_limit_microusd,'
            'label,created) VALUES (?,?,?,?,?,?)',
            ('legacy-' + pid, pid, 'CLOSED', None, 'legacy cost import', _now()))
    for legacy_key, pid, amount, origin in sources:
        db.execute(
            'INSERT OR IGNORE INTO cost_entries(id,project_id,run_id,stage,provider,'
            'model,pricing_version,reserved_microusd,billing_status,evidence,'
            'legacy_key,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, pid, 'legacy-' + pid, 'video', 'legacy', 'legacy',
             'legacy:' + origin, _to_micro(amount), 'unknown',
             'Imported pre-M1 reservation; historical undercount possible',
             legacy_key, _now(), _now()))
