"""The video handler must never turn a download problem into a second generation."""
from pathlib import Path

import pytest
from video_provider_core import CostEstimate, ProviderError, RemoteJob, RemoteStatus

from studio import attempts, core, jobs, ledger, worker
from studio.handlers import video


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Video handler', 20, 'mock', 5)
    run_id = ledger.open_run(pid, 5_000_000)
    return jobs.create(pid, kind='video', payload={
        'prompt': 'waves', 'duration_sec': 5, 'width': 1280, 'height': 720,
        'provider': 'fake', 'model': 'model', 'poll_interval_sec': 0,
    }, request_key='req', run_id=run_id)


class FakeProvider:
    submits = 0
    estimate = CostEstimate(10_000, 'test:1', 'test')
    submit_error = None
    status_result = RemoteStatus('succeeded', url='https://fal.media/video.mp4')

    def __init__(self, client):
        pass

    def estimate_cost(self, request):
        return self.estimate

    def submit_video(self, request, idempotency_key):
        type(self).submits += 1
        if self.submit_error:
            raise self.submit_error
        return RemoteJob('fake', 'remote-1')

    def status(self, job, *, model=None):
        return self.status_result


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeProvider.submits = 0
    FakeProvider.submit_error = None
    FakeProvider.status_result = RemoteStatus('succeeded', url='https://fal.media/video.mp4')
    FakeProvider.estimate = CostEstimate(10_000, 'test:1', 'test')
    monkeypatch.setitem(video.PROVIDERS, 'fake', FakeProvider)
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    return tmp_path


def fake_download(client, url, target):
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_bytes(b'fake-video')
    return {'duration': 5}


def test_video_handler_submits_polls_and_downloads(job, fake, monkeypatch):
    monkeypatch.setattr(video, 'download', fake_download)
    result = video.execute(job, attempts, worker.Control())
    assert FakeProvider.submits == 1
    assert result['remote_id'] == 'remote-1'
    assert Path(result['file']).read_bytes() == b'fake-video'
    assert attempts.list_for_job(job['id'])[0]['status'] == 'COMPLETED'
    with core.connect() as db:
        cost = db.execute('SELECT * FROM cost_entries').fetchone()
    assert cost['billing_status'] == 'reserved'
    assert cost['reserved_microusd'] == 10_000


def test_submit_timeout_marks_unknown_and_blocks(job, fake):
    FakeProvider.submit_error = ProviderError(
        'timeout', kind='unknown_remote', submission_state='maybe_sent')
    with pytest.raises(worker.JobBlocked) as caught:
        video.execute(job, attempts, worker.Control())
    assert caught.value.reason == 'unknown_remote'
    assert attempts.list_for_job(job['id'])[0]['status'] == 'UNKNOWN_REMOTE_STATE'
    assert ledger.committed(job['project_id']) == 10_000


def test_auth_error_blocks_without_retry(job, fake):
    FakeProvider.submit_error = ProviderError(
        'bad key', kind='auth', submission_state='not_sent')
    with pytest.raises(ProviderError):
        video.execute(job, attempts, worker.Control())
    assert attempts.list_for_job(job['id'])[0]['status'] == 'FAILED_FINAL'
    assert ledger.committed(job['project_id']) == 0


def test_download_failure_does_not_resubmit(job, fake, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('download interrupted')
    monkeypatch.setattr(video, 'download', fail)
    with pytest.raises(worker.JobBlocked) as caught:
        video.execute(job, attempts, worker.Control())
    assert caught.value.reason == 'download_failed'
    assert FakeProvider.submits == 1
    assert attempts.list_for_job(job['id'])[0]['status'] == 'DOWNLOADING'


def test_resume_uses_existing_remote_and_never_submits(job, fake, monkeypatch):
    monkeypatch.setattr(video, 'download', fake_download)
    attempt = attempts.create(job['id'], provider='fake', model='model', input_data={})
    attempts.prepare_submission(attempt['id'], amount_microusd=10_000,
                                pricing_version='test:1')
    attempts.attach_remote(attempt['id'], 'remote-1')
    attempts.mark_status(attempt['id'], 'POLLING')
    result = video.resume(job, attempts.get(attempt['id']), worker.Control())
    assert FakeProvider.submits == 0
    assert result['remote_id'] == 'remote-1'
    assert attempts.get(attempt['id'])['status'] == 'COMPLETED'


def test_resume_from_downloading_completes_without_submit(job, fake, monkeypatch):
    monkeypatch.setattr(video, 'download', fake_download)
    attempt = attempts.create(job['id'], provider='fake', model='model', input_data={})
    attempts.prepare_submission(attempt['id'], amount_microusd=10_000,
                                pricing_version='test:1')
    attempts.attach_remote(attempt['id'], 'remote-1')
    attempts.mark_status(attempt['id'], 'POLLING')
    attempts.mark_status(attempt['id'], 'DOWNLOADING')
    result = video.resume(job, attempts.get(attempt['id']), worker.Control())
    assert FakeProvider.submits == 0
    assert result['remote_id'] == 'remote-1'
    assert attempts.get(attempt['id'])['status'] == 'COMPLETED'


def test_missing_price_blocks_pricing_missing(job, fake):
    FakeProvider.estimate = None
    with pytest.raises(worker.JobBlocked) as caught:
        video.execute(job, attempts, worker.Control())
    assert caught.value.reason == 'pricing_missing'
    assert attempts.list_for_job(job['id']) == []


def test_pause_before_submit_makes_no_provider_call(job, fake):
    jobs.set_status(job['id'], 'RUNNING')
    jobs.pause(job['id'])
    with pytest.raises(worker.JobStopped):
        video.execute(job, attempts, worker.Control())
    assert FakeProvider.submits == 0
    assert attempts.list_for_job(job['id']) == []
