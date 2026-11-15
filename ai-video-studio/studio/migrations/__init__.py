"""Versioned SQLite migrations. Every applied migration is checksum-pinned."""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent
_FILE_RE = re.compile(r'^(\d{3})_([A-Za-z0-9_]+)\.(sql|py)$')
_LEGACY_TABLES = ('projects', 'shots', 'guided_attempts')


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: dict[int, Migration] = {}
    for path in sorted(Path(directory).iterdir()):
        match = _FILE_RE.match(path.name)
        if not match or not path.is_file():
            continue
        version = int(match.group(1))
        if version <= 0:
            raise ValueError(f'Invalid migration version: {path.name}')
        if version in found:
            raise ValueError(f'duplicate migration version {version}')
        found[version] = Migration(version, match.group(2), path, _checksum(path))
    return [found[version] for version in sorted(found)]


def _version_table_exists(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is not None


def _applied(db) -> dict[int, sqlite3.Row]:
    if not _version_table_exists(db):
        return {}
    return {row['version']: row for row in db.execute(
        'SELECT version,name,checksum FROM schema_migrations')}


def pending(db, directory: Path | None = None) -> list[Migration]:
    available = discover(directory or MIGRATIONS_DIR)
    applied = _applied(db)
    known = {item.version for item in available}
    for version in applied:
        if version not in known:
            raise RuntimeError(f'Applied migration {version} is missing from disk')
    for item in available:
        row = applied.get(item.version)
        if row is not None and row['checksum'] != item.checksum:
            raise RuntimeError(
                f'Migration {item.version} checksum changed; refusing to start')
    return [item for item in available if item.version not in applied]


def _has_legacy_data(db) -> bool:
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for name in _LEGACY_TABLES:
        if name in tables and db.execute(f'SELECT 1 FROM {name} LIMIT 1').fetchone():
            return True
    return False


def backup_database(db, db_path: Path, backup_root: Path) -> Path:
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = backup_root / f'pre-m1-{stamp}-{id(db):x}.sqlite3'
    with sqlite3.connect(target) as copy:
        db.backup(copy)
        result = copy.execute('PRAGMA quick_check').fetchone()
    if result is None or result[0] != 'ok':
        target.unlink(missing_ok=True)
        raise RuntimeError('Migration backup failed integrity check')
    return target


def _load_python(path: Path):
    name = f'studio_migration_{path.stem}_{id(path):x}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, 'apply', None)):
        raise ValueError(f'Python migration has no apply(db): {path.name}')
    return module


def _record(db, item: Migration) -> None:
    db.execute(
        'INSERT OR IGNORE INTO schema_migrations(version,name,checksum,applied_at) '
        'VALUES (?,?,?,?)',
        (item.version, item.name, item.checksum,
         datetime.now(timezone.utc).isoformat()),
    )


def apply(db, db_path: Path, *, directory: Path | None = None,
          backup_root: Path | None = None, backup: bool = True) -> list[int]:
    """Apply pending migrations. Caller must not hold an open transaction."""
    to_apply = pending(db, directory)
    if not to_apply:
        return []
    if backup and Path(db_path).exists() and _has_legacy_data(db):
        backup_database(db, Path(db_path),
                        Path(backup_root) if backup_root is not None
                        else Path(db_path).parent / 'backups')
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
        checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    db.commit()
    applied: list[int] = []
    for item in to_apply:
        if item.path.suffix == '.sql':
            db.executescript(item.path.read_text(encoding='utf-8'))
            _record(db, item)
            db.commit()
        else:
            module = _load_python(item.path)
            db.execute('BEGIN IMMEDIATE')
            try:
                module.apply(db)
                _record(db, item)
                db.commit()
            except Exception:
                db.rollback()
                raise
        applied.append(item.version)
    return applied
