"""Versioned migrations must be repeatable, backed up and tamper-evident."""
import hashlib
import importlib
import sqlite3
from pathlib import Path

import pytest

from studio import core, migrations


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


def test_failing_sql_migration_rolls_back_entirely(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    directory = make_migration_dir(tmp_path)
    (directory / '002_bad.sql').write_text(
        'CREATE TABLE IF NOT EXISTS should_not_survive(id TEXT);\n'
        'THIS IS NOT SQL;\n', encoding='utf-8')
    with open_db(db_path) as db:
        with pytest.raises(sqlite3.OperationalError):
            migrations.apply(db, db_path, directory=directory,
                             backup_root=tmp_path / 'backups')
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='should_not_survive'").fetchone() is None
        versions = [row[0] for row in db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
    assert versions == [1]


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
        tables = {row[0] for row in backup.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'runs' not in tables
        assert 'schema_migrations' not in tables
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


FOUNDATION = {
    'runs': ('id', 'project_id', 'status', 'budget_limit_microusd', 'label',
             'created', 'closed'),
    'jobs': ('id', 'project_id', 'run_id', 'kind', 'status', 'request_key',
             'payload_json', 'budget_limit_microusd', 'priority', 'lease_owner',
             'lease_expires', 'cancel_requested', 'block_reason', 'error_class',
             'error_detail', 'created', 'updated'),
    'job_attempts': ('id', 'job_id', 'attempt_no', 'provider', 'model', 'status',
                     'request_fingerprint', 'idempotency_key', 'remote_id',
                     'input_json', 'output_json', 'error_class', 'error_detail',
                     'retry_after_sec', 'created', 'updated'),
    'job_events': ('id', 'job_id', 'attempt_id', 'type', 'data_json', 'created'),
    'cost_entries': ('id', 'project_id', 'run_id', 'job_id', 'attempt_id', 'stage',
                     'provider', 'model', 'pricing_version', 'reserved_microusd',
                     'estimated_microusd', 'actual_microusd', 'billing_status',
                     'evidence', 'legacy_key', 'created', 'updated'),
}


@pytest.fixture
def foundation_db(tmp_path):
    db_path = tmp_path / 'studio.sqlite3'
    with open_db(db_path) as db:
        migrations.apply(db, db_path, backup_root=tmp_path / 'backups')
        yield db


def add_job(db, job_id='job-1', project='p1', key='key-1'):
    db.execute(
        'INSERT INTO jobs(id,project_id,kind,status,request_key,payload_json,created,updated) '
        "VALUES (?,?,'video','PENDING',?,'{}','now','now')",
        (job_id, project, key))


def add_attempt(db, attempt_id='a1', job='job-1', no=1, status='SUBMITTED',
                fingerprint='fp-1'):
    db.execute(
        'INSERT INTO job_attempts(id,job_id,attempt_no,provider,model,status,'
        'request_fingerprint,idempotency_key,input_json,created,updated) '
        "VALUES (?,?,?,'minimax','m',?,?,?,'{}','now','now')",
        (attempt_id, job, no, status, fingerprint, f'idem-{attempt_id}'))


def test_foundation_tables_and_columns_exist(foundation_db):
    for table, columns in FOUNDATION.items():
        actual = {row[1] for row in foundation_db.execute(f'PRAGMA table_info({table})')}
        assert set(columns) <= actual, table


def test_duplicate_job_request_key_is_rejected(foundation_db):
    add_job(foundation_db, 'job-1', 'p1', 'same')
    with pytest.raises(sqlite3.IntegrityError):
        add_job(foundation_db, 'job-2', 'p1', 'same')
    add_job(foundation_db, 'job-3', 'p2', 'same')


def test_duplicate_active_attempt_fingerprint_is_rejected(foundation_db):
    add_job(foundation_db)
    add_attempt(foundation_db, 'a1', 'job-1', 1, 'SUBMITTED', 'same-fp')
    with pytest.raises(sqlite3.IntegrityError):
        add_attempt(foundation_db, 'a2', 'job-1', 2, 'POLLING', 'same-fp')
    foundation_db.execute(
        "UPDATE job_attempts SET status='FAILED_RETRYABLE' WHERE id='a1'")
    add_attempt(foundation_db, 'a2', 'job-1', 2, 'SUBMITTED', 'same-fp')


def test_duplicate_cost_entry_per_attempt_stage_is_rejected(foundation_db):
    def add_cost(entry_id, attempt='a1', stage='video', legacy=None):
        foundation_db.execute(
            'INSERT INTO cost_entries(id,project_id,attempt_id,stage,provider,model,'
            'reserved_microusd,billing_status,legacy_key,created,updated) '
            "VALUES (?,? ,? ,?,'minimax','m',100,'reserved',?,'now','now')",
            (entry_id, 'p1', attempt, stage, legacy))
    add_cost('c1')
    with pytest.raises(sqlite3.IntegrityError):
        add_cost('c2')
    add_cost('c3', attempt=None, stage='video', legacy='shots:s1')
    with pytest.raises(sqlite3.IntegrityError):
        add_cost('c4', attempt=None, stage='video', legacy='shots:s1')


def legacy_database(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'studio.sqlite3'
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY, prompt TEXT, duration INTEGER,
                              provider TEXT, budget REAL, status TEXT, created TEXT);
        CREATE TABLE shots(id TEXT PRIMARY KEY, project_id TEXT, reserved_usd REAL);
        CREATE TABLE guided_attempts(id TEXT PRIMARY KEY, project_id TEXT,
                                     shot_id TEXT, reserved_usd REAL);
    """)
    db.execute("INSERT INTO projects VALUES ('legacy','x',20,'mock',5,'CREATED','now')")
    db.execute("INSERT INTO shots VALUES ('s1','legacy',1.5)")
    db.commit()
    db.close()
    return path


def test_connect_applies_migrations_to_new_database(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    with core.connect() as db:
        versions = [row[0] for row in db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
    assert versions == [1, 2, 3, 4, 5, 6]


def test_connect_migrates_existing_database_once(tmp_path, monkeypatch):
    legacy_database(tmp_path)
    monkeypatch.setattr(core, 'HOME', tmp_path)
    with core.connect() as db:
        first = db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0]
    with core.connect() as db:
        second = db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0]
        migrations_count = db.execute(
            'SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
    assert first == second == 1
    assert migrations_count == 6


def test_migration_006_initializes_project_state_without_revisions(tmp_path, monkeypatch):
    legacy_database(tmp_path)
    monkeypatch.setattr(core, 'HOME', tmp_path)
    with core.connect() as db:
        state = db.execute(
            'SELECT * FROM project_state WHERE project_id=?', ('legacy',)).fetchone()
    assert state is not None
    assert state['current_storyboard_revision_id'] is None
    assert state['current_timeline_revision_id'] is None
    with core.connect() as db:
        # 006 only initializes project_state; it must never probe media or
        # create revisions.
        assert db.execute('SELECT COUNT(*) FROM storyboard_revisions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM timeline_revisions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM shot_revisions').fetchone()[0] == 0



def test_connect_creates_backup_before_upgrading_existing_database(tmp_path, monkeypatch):
    legacy_database(tmp_path)
    monkeypatch.setattr(core, 'HOME', tmp_path)
    with core.connect():
        pass
    backups = sorted((tmp_path / 'backups').glob('pre-m1-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT id FROM projects').fetchone()[0] == 'legacy'


