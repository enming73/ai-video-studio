"""Legacy reserved amounts are imported once; authorization ceilings are not spend."""
import sqlite3
from pathlib import Path

import pytest

from studio import core, ledger, migrations


def legacy_db(tmp_path: Path, budget=10.0):
    path = tmp_path / 'studio.sqlite3'
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY, prompt TEXT, duration INTEGER,
                              provider TEXT, budget REAL, status TEXT, created TEXT);
        CREATE TABLE shots(id TEXT PRIMARY KEY, project_id TEXT, seq INTEGER,
                           prompt TEXT, duration INTEGER, status TEXT, file TEXT,
                           error TEXT, attempts INTEGER, remote_id TEXT,
                           video_url TEXT, reserved_usd REAL DEFAULT 0,
                           archived INTEGER DEFAULT 0);
        CREATE TABLE guided_attempts(id TEXT PRIMARY KEY, project_id TEXT,
                                     shot_id TEXT, reserved_usd REAL DEFAULT 0);
        CREATE TABLE guided_image_attempts(id TEXT PRIMARY KEY, project_id TEXT,
                                           budget REAL);
        CREATE TABLE guided_planning_attempts(project_id TEXT, request_id TEXT,
                                              budget_usd REAL);
        CREATE TABLE guided_boundary_prompt_attempts(id TEXT PRIMARY KEY,
                                                     project_id TEXT, budget_usd REAL);
    """)
    db.execute("INSERT INTO projects VALUES ('p1','x',20,'mock',?,'CREATED','now')", (budget,))
    db.commit()
    db.close()
    return path


def apply_all(path: Path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    migrations.apply(db, path)
    return db


def test_legacy_amounts_count_once(tmp_path):
    path = legacy_db(tmp_path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO shots(id,project_id,seq,reserved_usd) VALUES ('s1','p1',1,1.5)")
    db.execute("INSERT INTO shots(id,project_id,seq,reserved_usd) VALUES ('s2','p1',2,0)")
    db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,reserved_usd) "
               "VALUES ('g1','p1','s2',2.5)")
    db.commit()
    db.close()
    db = apply_all(path)
    first = db.execute('SELECT COUNT(*), COALESCE(SUM(reserved_microusd),0) '
                       'FROM cost_entries').fetchone()
    assert first[0] == 2
    assert first[1] == 4_000_000
    migrations.apply(db, path)
    second = db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0]
    assert second == 2
    run = db.execute("SELECT status FROM runs WHERE id='legacy-p1'").fetchone()
    assert run['status'] == 'CLOSED'
    db.close()


def test_guided_attempt_takes_precedence_over_shot_amount(tmp_path):
    path = legacy_db(tmp_path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO shots(id,project_id,seq,reserved_usd) VALUES ('s1','p1',1,1.0)")
    db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,reserved_usd) "
               "VALUES ('g1','p1','s1',1.0)")
    db.commit()
    db.close()
    db = apply_all(path)
    rows = db.execute('SELECT legacy_key FROM cost_entries').fetchall()
    assert [row[0] for row in rows] == ['guided_attempts:g1']
    db.close()


def test_legacy_ceilings_are_not_migrated_as_spend(tmp_path):
    path = legacy_db(tmp_path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO guided_image_attempts VALUES ('i1','p1',5.0)")
    db.execute("INSERT INTO guided_planning_attempts VALUES ('p1','r1',3.0)")
    db.execute("INSERT INTO guided_boundary_prompt_attempts VALUES ('b1','p1',2.0)")
    db.commit()
    db.close()
    db = apply_all(path)
    assert db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    db.close()


def test_legacy_over_budget_blocks_new_reservation(tmp_path, monkeypatch):
    path = legacy_db(tmp_path, budget=1.0)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO shots(id,project_id,seq,reserved_usd) VALUES ('s1','p1',1,1.5)")
    db.commit()
    db.close()
    monkeypatch.setattr(core, 'HOME', tmp_path)
    db = apply_all(path)
    db.close()
    run_id = ledger.open_run('p1', 10_000_000)
    with pytest.raises(ledger.BudgetExceeded) as caught:
        ledger.reserve('p1', stage='video', provider='mock', model='mock',
                       amount_microusd=100_000, pricing_version='test-v1',
                       run_id=run_id)
    assert caught.value.scope == 'project'
    assert caught.value.committed_microusd == 1_500_000


def test_missing_legacy_tables_are_skipped(tmp_path):
    path = tmp_path / 'studio.sqlite3'
    db = sqlite3.connect(path)
    migrations.apply(db, path)
    assert db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0] == 0
    db.close()
