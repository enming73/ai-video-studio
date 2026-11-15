"""Leases and recovery must never turn an unknown submission into a retry."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from studio import attempts, core, jobs, ledger, worker


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


def test_reclaimed_job_with_stop_request_is_not_executed(job):
    jobs.claim('worker-a', lease_seconds=60)
    jobs.pause(job['id'])
    assert jobs.claim('worker-b', now='9999-01-01T00:00:00+00:00') is None
    assert jobs.get(job['id'])['status'] == 'PAUSED'


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


class Handler:
    def __init__(self, result=None, error=None):
        self.result = result or {'ok': True}
        self.error = error
        self.calls = 0

    def execute(self, job, attempt_service, control):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def resume(self, job, attempt, control):
        self.calls += 1
        return self.result


def test_worker_executes_registered_handler_and_succeeds(job):
    handler = Handler({'file': '/tmp/x.mp4'})
    worker.register('video', handler)
    assert worker.run_once('w') == job['id']
    assert handler.calls == 1
    saved = jobs.get(job['id'])
    assert saved['status'] == 'SUCCEEDED'
    assert 'x.mp4' in saved['output_json']


def test_worker_blocks_unknown_recovery_without_calling_handler(job):
    _submitting(job)
    handler = Handler()
    worker.register('video', handler)
    worker.run_once('w')
    assert handler.calls == 0
    saved = jobs.get(job['id'])
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    assert jobs.latest_attempt(job['id'])['status'] == 'UNKNOWN_REMOTE_STATE'


def test_worker_marks_failed_on_non_provider_exception(job):
    worker.register('video', Handler(error=RuntimeError('boom')))
    worker.run_once('w')
    saved = jobs.get(job['id'])
    assert saved['status'] == 'FAILED'
    assert saved['error_class'] == 'RuntimeError'


def test_worker_heartbeat_runs_during_slow_handler(job):
    seen = {}

    class Slow:
        def execute(self, record, attempt_service, control):
            seen['before'] = jobs.get(record['id'])['lease_expires']
            time.sleep(0.3)
            seen['after'] = jobs.get(record['id'])['lease_expires']
            return {}

    worker.register('video', Slow())
    worker.run_once('w', lease_seconds=1, heartbeat_seconds=0.05)
    assert seen['after'] > seen['before']
    assert jobs.get(job['id'])['status'] == 'SUCCEEDED'


def test_worker_respects_pause_and_picks_next_job(job):
    paused = jobs.pause(job['id'])
    assert paused['status'] == 'PAUSED'
    second = jobs.create(job['project_id'], kind='video', payload={},
                         request_key='req-2', run_id=job['run_id'])
    handler = Handler()
    worker.register('video', handler)
    assert worker.run_once('w') == second['id']
    assert jobs.get(job['id'])['status'] == 'PAUSED'


def test_worker_pauses_at_checkpoint(job):
    class Pausing:
        def execute(self, record, attempt_service, control):
            jobs.pause(record['id'])
            control.checkpoint(record['id'])
            return {'never': True}

    worker.register('video', Pausing())
    worker.run_once('w')
    saved = jobs.get(job['id'])
    assert saved['status'] == 'PAUSED'
    assert 'remote may continue' in saved['output_json']


def test_worker_cli_once(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from studio.cli import app
    monkeypatch.setattr(core, 'HOME', tmp_path)
    result = CliRunner().invoke(app, ['worker', '--once'])
    assert result.exit_code == 0


def test_worker_stops_before_submit_when_paused(job):
    class Pausing:
        def execute(self, record, service, control):
            jobs.pause(record['id'])
            control.checkpoint(record['id'])
            return {'should_not': True}
    worker.register('video', Pausing())
    worker.run_once('w')
    assert jobs.get(job['id'])['status'] == 'PAUSED'


def test_worker_does_not_overwrite_after_lease_loss(job, monkeypatch):
    monkeypatch.setattr(jobs, 'heartbeat', lambda *args, **kwargs: False)

    class Slow:
        def execute(self, record, service, control):
            time.sleep(0.1)
            return {'late': True}

    worker.register('video', Slow())
    worker.run_once('w', lease_seconds=1, heartbeat_seconds=0.01)
    assert jobs.get(job['id'])['status'] == 'RUNNING'
    with core.connect() as db:
        assert db.execute("SELECT 1 FROM job_events WHERE job_id=? AND type='lease_lost'",
                          (job['id'],)).fetchone() is not None


def test_worker_retries_retryable_not_sent_failure(job):
    from video_provider_core import ProviderError

    class Failing:
        def execute(self, record, service, control):
            from studio import attempts, ledger
            attempt = service.create(record['id'], provider='fake', model='m', input_data={})
            service.prepare_submission(attempt['id'], amount_microusd=1_000,
                                       pricing_version='v1')
            service.mark_failed(attempt['id'], 'FAILED_RETRYABLE', '429',
                                error_class='rate_limited')
            raise ProviderError('429', kind='rate_limited', submission_state='not_sent')

    worker.register('video', Failing())
    worker.run_once('w')
    assert jobs.get(job['id'])['status'] == 'PENDING'


def test_worker_maps_budget_failure_to_blocked(job):
    from studio import ledger as ledger_module

    class Broke:
        def execute(self, record, service, control):
            raise ledger_module.BudgetExceeded('project', 10, 5, 1)

    worker.register('video', Broke())
    worker.run_once('w')
    assert jobs.get(job['id'])['status'] == 'BLOCKED'
    assert jobs.get(job['id'])['block_reason'] == 'budget_exceeded'


def test_worker_blocks_generic_error_after_submitting(job):
    class Broken:
        def execute(self, record, service, control):
            attempt = service.create(record['id'], provider='fake', model='m',
                                     input_data={})
            service.prepare_submission(attempt['id'], amount_microusd=1_000,
                                       pricing_version='v1')
            raise RuntimeError('unexpected after submit')

    worker.register('video', Broken())
    worker.run_once('w')
    saved = jobs.get(job['id'])
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    assert jobs.latest_attempt(job['id'])['status'] == 'UNKNOWN_REMOTE_STATE'


def test_worker_cancel_at_checkpoint_keeps_remote_warning(job):
    class Cancelling:
        def execute(self, record, service, control):
            jobs.cancel(record['id'])
            control.checkpoint(record['id'])
            return {'never': True}

    worker.register('video', Cancelling())
    worker.run_once('w')
    saved = jobs.get(job['id'])
    assert saved['status'] == 'CANCELLED'
    assert 'remote may continue' in saved['output_json']
