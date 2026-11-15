"""Versioned migrations must be repeatable, backed up and tamper-evident."""
import hashlib
import importlib
import sqlite3
from pathlib import Path

import pytest

from studio import migrations


def make_migration_dir(tmp_path: Path) -> Path:
    directory = tmp_path / 'mlocal'
    directory.mkdir()
    (directory / '001_sample.sql').write_text(
        'CREATE TABLE IF NOT EXISTS sample(id TEXT PRIMARY KEY);\n', encoding='utf-8')
    return directory


def open_db(path: Path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def test_fresh_database_records_applied_versions(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    with open_db(db_path) as db:
        assert migrations.apply(db, db_path, directory=directory,
                                backup_root=tmp_path / 'backups') == [1]
        row = db.execute('SELECT version,name,checksum FROM schema_migrations').fetchone()
    assert row['version'] == 1
    assert row['name'] == 'sample'
    assert row['checksum'] == hashlib.sha256(
        (directory / '001_sample.sql').read_bytes()).hexdigest()


def test_second_apply_does_not_reapply(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    with open_db(db_path) as db:
        migrations.apply(db, db_path, directory=directory, backup_root=tmp_path / 'backups')
        assert migrations.apply(db, db_path, directory=directory,
                                backup_root=tmp_path / 'backups') == []
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 1


def test_changed_checksum_blocks_startup(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    with open_db(db_path) as db:
        migrations.apply(db, db_path, directory=directory, backup_root=tmp_path / 'backups')
        path = directory / '001_sample.sql'
        path.write_text(path.read_text() + '-- edited\n', encoding='utf-8')
        with pytest.raises(RuntimeError, match='1'):
            migrations.pending(db, directory=directory)


def test_empty_database_is_not_backed_up(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    backup_root = tmp_path / 'backups'
    with open_db(db_path) as db:
        migrations.apply(db, db_path, directory=directory, backup_root=backup_root)
    assert not backup_root.exists()


def test_existing_database_is_backed_up_before_migration(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    with open_db(db_path) as db:
        db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO projects VALUES ('legacy-project')")
        db.commit()
    backup_root = tmp_path / 'backups'
    with open_db(db_path) as db:
        migrations.apply(db, db_path, directory=directory, backup_root=backup_root)
    backups = sorted(backup_root.glob('pre-m1-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT id FROM projects').fetchone()[0] == 'legacy-project'
    with open_db(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 1


def test_backup_failure_aborts_before_schema_change(tmp_path, monkeypatch):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    with open_db(db_path) as db:
        db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO projects VALUES ('unchanged')")
        db.commit()

        def fail(*args, **kwargs):
            raise OSError('backup unavailable')

        monkeypatch.setattr(migrations, 'backup_database', fail)
        with pytest.raises(OSError, match='backup unavailable'):
            migrations.apply(db, db_path, directory=directory,
                             backup_root=tmp_path / 'backups')
        assert db.execute('SELECT id FROM projects').fetchone()[0] == 'unchanged'
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is None
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sample'"
        ).fetchone() is None


def test_duplicate_versions_are_rejected(tmp_path):
    directory = make_migration_dir(tmp_path)
    (directory / '001_other.sql').write_text('SELECT 1;\n', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate'):
        migrations.discover(directory)


def test_python_migration_runs_once(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    (directory / '002_data.py').write_text(
        'def apply(db):\n'
        "    db.execute('CREATE TABLE IF NOT EXISTS python_sample(id TEXT)')\n"
        "    db.execute(\"INSERT INTO python_sample VALUES ('one')\")\n",
        encoding='utf-8')
    with open_db(db_path) as db:
        assert migrations.apply(db, db_path, directory=directory,
                                backup_root=tmp_path / 'backups') == [1, 2]
        migrations.apply(db, db_path, directory=directory, backup_root=tmp_path / 'backups')
        assert db.execute('SELECT COUNT(*) FROM python_sample').fetchone()[0] == 1
