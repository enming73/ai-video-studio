"""Attempt transitions and reservations must commit together."""
import pytest

from studio import attempts, core, jobs, ledger


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Attempt project', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 100_000)
    record = jobs.create(pid, kind='video', payload={'prompt': 'p'},
                         request_key='req', run_id=run_id)
    return record, run_id


def add(job, data=None):
    return attempts.create(job['id'], provider='fake', model='m',
                           input_data=data or {'prompt': 'p', 'duration': 5})


def test_attempt_numbers_and_idempotency_key_are_unique(job):
    record, _ = job
    first = add(record)
    second = add(record, {'prompt': 'other'})
    assert (first['attempt_no'], second['attempt_no']) == (1, 2)
    assert first['idempotency_key'] != second['idempotency_key']
    assert first['request_fingerprint'] != second['request_fingerprint']


def test_duplicate_active_fingerprint_reuses_attempt(job):
    record, _ = job
    first = add(record)
    again = add(record)
    assert again['id'] == first['id']
    assert len(attempts.list_for_job(record['id'])) == 1


def test_prepare_submission_is_atomic(job):
    record, _ = job
    attempt = add(record)
    with pytest.raises(ledger.BudgetExceeded):
        attempts.prepare_submission(attempt['id'], amount_microusd=200_000,
                                    pricing_version='v1')
    assert attempts.get(attempt['id'])['status'] == 'PLANNED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM cost_entries').fetchone()[0] == 0


def test_prepare_submission_reserves_and_sets_submitting(job):
    record, _ = job
    attempt = add(record)
    entry = attempts.prepare_submission(attempt['id'], amount_microusd=50_000,
                                        pricing_version='v1')
    assert attempts.get(attempt['id'])['status'] == 'SUBMITTING'
    with core.connect() as db:
        row = db.execute('SELECT * FROM cost_entries WHERE id=?', (entry,)).fetchone()
    assert row['billing_status'] == 'reserved'
    assert row['reserved_microusd'] == 50_000
    assert ledger.committed(record['project_id']) == 50_000


def test_illegal_transition_is_rejected(job):
    record, _ = job
    attempt = add(record)
    with pytest.raises(ValueError, match='transition'):
        attempts.transition(attempt['id'], 'COMPLETED')


def test_unknown_submission_cannot_be_prepared_again(job):
    record, _ = job
    attempt = add(record)
    attempts.prepare_submission(attempt['id'], amount_microusd=10_000,
                                pricing_version='v1')
    attempts.mark_unknown(attempt['id'], 'timeout')
    assert attempts.get(attempt['id'])['status'] == 'UNKNOWN_REMOTE_STATE'
    with pytest.raises(ValueError):
        attempts.prepare_submission(attempt['id'], amount_microusd=10_000,
                                    pricing_version='v1')
