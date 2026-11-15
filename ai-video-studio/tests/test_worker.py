"""Leases and recovery must never turn an unknown submission into a retry."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio import attempts, core, jobs, ledger


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Lease project', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    return jobs.create(pid, kind='video', payload={}, request_key='req', run_id=run_id)


def test_claim_marks_running_with_lease(job):
    claimed = jobs.claim('worker-a', lease_seconds=60)
    assert claimed['id'] == job['id']
    assert claimed['status'] == 'RUNNING'
    assert claimed['lease_owner'] == 'worker-a'
    assert claimed['lease_expires']
    assert claimed['recovered'] is False
    assert jobs.claim('worker-b') is None


def test_two_workers_claim_one_job(job):
    barrier = threading.Barrier(8)
    def claim(index):
        barrier.wait()
        return jobs.claim(f'w{index}')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    claimed = [item for item in results if item]
    assert len(claimed) == 1
    assert claimed[0]['id'] == job['id']


def test_expired_lease_can_be_reclaimed(job):
    first = jobs.claim('worker-a', lease_seconds=60)
    assert jobs.claim('worker-b', now='9999-01-01T00:00:00+00:00')['recovered'] is True
    assert jobs.get(job['id'])['lease_owner'] == 'worker-b'
    assert first['id'] == job['id']


def test_heartbeat_extends_only_for_owner(job):
    claimed = jobs.claim('worker-a', lease_seconds=60)
    before = claimed['lease_expires']
    assert jobs.heartbeat(job['id'], 'worker-a', lease_seconds=120) is True
    assert jobs.get(job['id'])['lease_expires'] != before
    assert jobs.heartbeat(job['id'], 'worker-b') is False


def _submitting(job):
    record = attempts.create(job['id'], provider='fake', model='m', input_data={})
    attempts.prepare_submission(record['id'], amount_microusd=10_000,
                                pricing_version='v1')
    return attempts.get(record['id'])


def test_recovery_blocks_submitting_without_remote(job):
    attempt = _submitting(job)
    assert jobs.recovery_action(job['id']) == 'block_unknown'
    assert attempt['remote_id'] is None


def test_recovery_resumes_remote_job_without_resubmit(job):
    attempt = _submitting(job)
    attempts.attach_remote(attempt['id'], 'remote-1')
    attempts.mark_status(attempt['id'], 'POLLING')
    assert jobs.recovery_action(job['id']) == 'resume'


def test_recovery_retries_only_retryable_failure_below_limit(job):
    attempt = _submitting(job)
    attempts.mark_failed(attempt['id'], 'FAILED_RETRYABLE', 'definite rejection')
    assert jobs.recovery_action(job['id'], max_attempts=2) == 'execute'
    assert jobs.recovery_action(job['id'], max_attempts=1) == 'fail'
