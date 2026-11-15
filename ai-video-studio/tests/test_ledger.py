"""Money is integer micro-USD; reservations are atomic and never overdraw."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from studio import core, ledger, migrations


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Ledger project', 20, 'mock', 1.0)
    with core.connect() as db:
        migrations.apply(db, tmp_path / 'studio.sqlite3')
    return pid


def reserve(pid, amount, run_id=None, stage='video'):
    return ledger.reserve(
        pid, stage=stage, provider='mock', model='mock',
        amount_microusd=amount, pricing_version='test-v1',
        run_id=run_id, estimated_microusd=amount)


def test_money_conversion_is_exact_and_rejects_invalid():
    assert ledger.to_micro('0.1') == 100_000
    assert ledger.to_micro(1e-7) == 0
    assert ledger.to_micro(Decimal('0.0000005')) == 1
    assert ledger.to_usd(123_456) == 0.123456
    for bad in (True, float('nan'), float('inf'), -1, '-0.1', 'abc'):
        with pytest.raises(ValueError):
            ledger.to_micro(bad)


def test_reserve_creates_reserved_entry(project):
    run_id = ledger.open_run(project, 1_000_000, label='test')
    entry_id = reserve(project, 250_000, run_id)
    with core.connect() as db:
        row = db.execute('SELECT * FROM cost_entries WHERE id=?', (entry_id,)).fetchone()
    assert row['reserved_microusd'] == 250_000
    assert row['billing_status'] == 'reserved'
    assert row['pricing_version'] == 'test-v1'
    assert row['run_id'] == run_id


def test_project_budget_exceeded_is_atomic(project):
    run_id = ledger.open_run(project, 10_000_000)
    reserve(project, 600_000, run_id)
    with pytest.raises(ledger.BudgetExceeded) as caught:
        reserve(project, 500_000, run_id)
    assert caught.value.scope == 'project'
    assert caught.value.committed_microusd == 600_000
    assert caught.value.limit_microusd == 1_000_000
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0] == 1


def test_run_budget_is_enforced_independently(project):
    run_id = ledger.open_run(project, 500_000)
    with pytest.raises(ledger.BudgetExceeded) as caught:
        reserve(project, 600_000, run_id)
    assert caught.value.scope == 'run'
    assert caught.value.limit_microusd == 500_000


def test_closed_run_rejects_reservation(project):
    run_id = ledger.open_run(project, 1_000_000)
    ledger.close_run(run_id)
    with pytest.raises(ValueError, match='not open'):
        reserve(project, 100_000, run_id)


def test_concurrent_reservations_do_not_overdraw(project):
    run_id = ledger.open_run(project, 10_000_000)
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        try:
            reserve(project, 200_000, run_id)
            return True
        except ledger.BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(8)))
    assert results.count(True) == 5
    with core.connect() as db:
        total = db.execute(
            "SELECT COALESCE(SUM(reserved_microusd),0) FROM cost_entries "
            "WHERE billing_status='reserved'").fetchone()[0]
    assert total == 1_000_000


def test_reservation_requires_positive_integer_with_price_version(project):
    run_id = ledger.open_run(project, 1_000_000)
    with pytest.raises(ValueError):
        reserve(project, 0, run_id)
    with pytest.raises(ValueError):
        ledger.reserve(project, stage='video', provider='mock', model='mock',
                       amount_microusd=1, pricing_version='', run_id=run_id)


def test_settle_replaces_reservation_in_committed(project):
    run_id = ledger.open_run(project, 1_000_000)
    entry = reserve(project, 500_000, run_id)
    ledger.settle(entry, 300_000, 'provider usage record')
    assert ledger.committed(project) == 300_000
    summary = ledger.summary(project)
    assert summary['open_exposure_microusd'] == 0
    assert summary['settled_microusd'] == 300_000


def test_unknown_keeps_exposure(project):
    run_id = ledger.open_run(project, 1_000_000)
    entry = reserve(project, 400_000, run_id)
    ledger.mark_unknown(entry, 'request timed out')
    assert ledger.committed(project) == 400_000
    assert ledger.summary(project)['unknown_count'] == 1


def test_release_requires_evidence_and_clears_exposure(project):
    run_id = ledger.open_run(project, 1_000_000)
    entry = reserve(project, 400_000, run_id)
    ledger.mark_unknown(entry)
    with pytest.raises(ValueError, match='evidence'):
        ledger.release(entry, '')
    ledger.release(entry, 'dashboard shows no task')
    assert ledger.committed(project) == 0


def test_terminal_entry_cannot_be_transitioned_again(project):
    run_id = ledger.open_run(project, 1_000_000)
    entry = reserve(project, 400_000, run_id)
    ledger.settle(entry, 350_000, 'invoice')
    with pytest.raises(ValueError, match='not transitionable'):
        ledger.release(entry, 'late evidence')
    with pytest.raises(ValueError, match='not transitionable'):
        ledger.settle(entry, 1, 'again')


def test_settle_rejects_negative_or_boolean_actual(project):
    run_id = ledger.open_run(project, 1_000_000)
    entry = reserve(project, 400_000, run_id)
    for bad in (-1, True):
        with pytest.raises(ValueError):
            ledger.settle(entry, bad, 'invoice')


def test_summary_groups_by_stage(project):
    run_id = ledger.open_run(project, 1_000_000)
    video = reserve(project, 200_000, run_id, 'video')
    image = reserve(project, 100_000, run_id, 'image')
    tts = reserve(project, 50_000, run_id, 'tts')
    ledger.settle(image, 80_000, 'invoice')
    ledger.mark_unknown(tts)
    summary = ledger.summary(project)
    assert summary['settled_microusd'] == 80_000
    assert summary['open_exposure_microusd'] == 250_000
    assert summary['committed_microusd'] == 330_000
    assert summary['by_stage']['video']['committed_microusd'] == 200_000
    assert summary['by_stage']['image']['committed_microusd'] == 80_000
    assert summary['by_stage']['tts']['unknown_count'] == 1

