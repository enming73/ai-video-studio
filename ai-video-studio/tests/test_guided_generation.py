from pathlib import Path
import json

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import subprocess

from studio import core, guided_boundaries as boundaries, guided_characters as characters, guided_generation as generation, guided_images as images, guided_workflow as workflow, provider_capabilities, timeline
from tests.helpers import add_version, attach_video


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant explores a river', 20, 'mock', 4)
    workflow.create(pid)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant explores a forest.', 'duration': 10},
        {'prompt': 'An elephant approaches a river.', 'duration': 10}]}, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    monkeypatch.setattr(core, 'mock_generate', lambda shot, path: path.write_bytes(b'generated'))
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    return pid, tmp_path


@pytest.fixture
def video_project(boundary_project, monkeypatch):
    pid, state = boundary_project
    generation.set_video_model(pid, 'mock', 'mock', workflow.get(pid)['revision'])
    monkeypatch.setattr(generation, '_mock_frame_video',
                        lambda shot, target, *args, **kwargs: target.write_bytes(b'generated'))
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    return pid, workflow.get(pid)


def confirm_pair(pid, state, *positions):
    for position in positions:
        boundary = state['boundaries'][position]
        version = add_version(pid, boundary['id'], f'b{position}', size='1536x864')
        boundaries.confirm(pid, boundary['id'], version, workflow.get(pid)['revision'])
    return workflow.get(pid)


@pytest.fixture
def selected_fresh_video(tmp_path, monkeypatch):
    """Single-shot character project with a generated, selected, current boundary video."""
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = characters.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    generation.set_video_model(pid, 'mock', 'mock', workflow.get(pid)['revision'])
    monkeypatch.setattr(generation, '_mock_frame_video',
                        lambda shot, target, *a, **k: target.write_bytes(b'generated'))
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    state = workflow.get(pid)
    for i in (0, 1):
        version = add_version(pid, state['boundaries'][i]['id'], f'b{i}', size='1536x864')
        boundaries.confirm(pid, state['boundaries'][i]['id'], version, workflow.get(pid)['revision'])
    attempt = generation.submit(pid, 1, 1, confirm_paid=True, request_id='fresh')
    assert attempt['status'] == 'COMPLETED'
    asset_id = attempt['asset_id']
    timeline.select(pid, 1, asset_id)
    return pid, asset_id


def test_selecting_fresh_boundary_video_is_current_and_render_ready(selected_fresh_video):
    pid, asset_id = selected_fresh_video
    shot = workflow.get(pid)['shots'][0]
    assert shot['old_video_decision_required'] is False
    workflow.confirm_shot(pid, 1, asset_id, workflow.get(pid)['revision'])
    assert workflow.render_ready(pid)


def test_selecting_older_asset_marks_stale(selected_fresh_video):
    pid, _ = selected_fresh_video
    shot = workflow.get(pid)['shots'][0]
    attach_video(pid, shot, 'old-start', 'old-end')
    assert workflow.get(pid)['shots'][0]['old_video_decision_required'] is True


def test_reselecting_current_asset_clears_stale(selected_fresh_video):
    pid, fresh_asset_id = selected_fresh_video
    shot = workflow.get(pid)['shots'][0]
    attach_video(pid, shot, 'old-start', 'old-end')
    assert workflow.get(pid)['shots'][0]['old_video_decision_required'] is True
    timeline.select(pid, 1, fresh_asset_id)
    assert workflow.get(pid)['shots'][0]['old_video_decision_required'] is False


def test_first_last_frame_capability_required(video_project, monkeypatch):
    pid, state = video_project
    state = confirm_pair(pid, state, 0, 1)
    cap = provider_capabilities.i2v_capabilities('mock', 'mock')
    monkeypatch.setattr(provider_capabilities, 'i2v_capabilities',
                        lambda p, m: {**cap, 'first_last_frame': False})
    with pytest.raises(ValueError, match='first'):
        generation.preview(pid, 1)


def test_any_shot_with_both_boundaries_can_generate_first(video_project):
    pid, state = video_project
    state = confirm_pair(pid, state, 1, 2)
    assert generation.preview(pid, 2)['last_frame_id'] == state['boundaries'][2]['confirmed_image_id']
    attempt = generation.submit(pid, 2, 1, confirm_paid=True, request_id='second')
    assert attempt['status'] == 'COMPLETED'
    assert timeline.assets(pid, 1) == []


def test_missing_boundary_blocks_generation(video_project):
    pid, _ = video_project
    with pytest.raises(ValueError, match='boundary'):
        generation.preview(pid, 1)


def test_provenance_records_both_boundaries(video_project):
    pid, state = video_project
    state = confirm_pair(pid, state, 0, 1)
    attempt = generation.submit(pid, 1, 1, confirm_paid=True, request_id='first')
    with core.connect() as db:
        row = db.execute('SELECT frame_id,last_frame_id FROM guided_video_provenance WHERE attempt_id=?',
                         (attempt['id'],)).fetchone()
    assert (row['frame_id'], row['last_frame_id']) == (
        state['boundaries'][0]['confirmed_image_id'], state['boundaries'][1]['confirmed_image_id'])


def test_mock_regeneration_preserves_versions_and_idempotency(project):
    pid, _ = project
    first = generation.submit(pid, 1, 0, confirm_paid=False, request_id='first')
    repeated = generation.submit(pid, 1, 0, confirm_paid=False, request_id='first')
    second = generation.submit(pid, 1, 0, confirm_paid=False, request_id='second')
    assert first['id'] == repeated['id']
    assert first['id'] != second['id']
    assert first['file'] != second['file']
    assert len(timeline.assets(pid, 1)) == 2
    assert open(first['file'], 'rb').read() == b'generated'


def test_paid_authorization_required_before_submission(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 2, confirm_paid=False, request_id='no')
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 0.5, confirm_paid=True, request_id='low')
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_attempts').fetchone()[0] == 0


def test_failed_submission_is_ambiguous_and_cannot_resubmit(project, monkeypatch):
    from studio import jobs, worker
    from studio.handlers import video
    from video_provider_core import CostEstimate, ProviderError
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    calls = []
    class Provider:
        def __init__(self, client): pass
        def estimate_cost(self, request):
            return CostEstimate(1_000_000, 'test:v1', 'test')
        def submit_video(self, request, idempotency_key):
            calls.append(request.prompt)
            raise ProviderError('network uncertain', kind='unknown_remote', submission_state='maybe_sent')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    monkeypatch.setitem(worker.HANDLERS, 'video', video)
    state = generation.submit(pid, 1, 2, confirm_paid=True, request_id='ambiguous')
    assert state['status'] == 'QUEUED'
    worker.run_once()
    assert jobs.get(state['id'])['block_reason'] == 'unknown_remote'
    duplicate = generation.submit(pid, 1, 2, confirm_paid=True, request_id='different')
    assert duplicate['status'] == 'SUBMITTING'
    worker.run_once()
    assert len(calls) == 1


def test_concurrent_duplicate_request_submits_only_once(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    ready = Barrier(2)
    def submit():
        ready.wait(timeout=5)
        return generation.submit(pid, 1, 2, confirm_paid=True, request_id='same')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(submit), pool.submit(submit)
        assert first.result(timeout=10) == second.result(timeout=10)
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM guided_attempts').fetchone()[0] == 0


def test_mock_frame_video_produces_real_two_half_clip(boundary_project):
    pid, state = boundary_project
    generation.set_video_model(pid, 'mock', 'mock', workflow.get(pid)['revision'])
    state = confirm_pair(pid, state, 0, 1)
    attempt = generation.submit(pid, 1, 0, confirm_paid=False, request_id='real-ffmpeg')
    assert attempt['status'] == 'COMPLETED'
    path = Path(attempt['file'])
    assert path.is_file() and path.stat().st_size > 0
    core.verify_video(path, state['shots'][0]['duration'])


def test_paid_submit_creates_job_with_boundary_paths(boundary_project, monkeypatch):
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('UPDATE projects SET budget=5 WHERE id=?', (pid,))
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', workflow.get(pid)['revision'])
    state = confirm_pair(pid, state, 0, 1)
    first_bytes = images.image_path(pid, state['boundaries'][0]['confirmed_image_id']).read_bytes()
    last_bytes = images.image_path(pid, state['boundaries'][1]['confirmed_image_id']).read_bytes()
    attempt = generation.submit(pid, 1, 1, confirm_paid=True, request_id='i2v')
    assert attempt['status'] == 'QUEUED'
    from studio import jobs
    from studio.handlers import video
    job = jobs.get(attempt['id'])
    payload = json.loads(job['payload_json'])
    request = video._request(job, payload, core.HOME / pid)
    assert request.first_frame == first_bytes
    assert request.last_frame == last_bytes
    assert request.first_frame_mime == request.last_frame_mime == 'image/png'
    assert request.duration_sec == state['shots'][0]['duration']
    assert payload['guided_request_id'] == 'i2v'
    assert payload['first_frame_id'] == state['boundaries'][0]['confirmed_image_id']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_attempts').fetchone()[0] == 0
        assert db.execute('SELECT budget_limit_microusd FROM runs WHERE id=?',
                          (job['run_id'],)).fetchone()[0] == 1_000_000


def test_submit_is_idempotent_by_request_id(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    first = generation.submit(pid, 1, 2, confirm_paid=True, request_id='once')
    assert first['status'] == 'QUEUED'
    assert generation.submit(pid, 1, 2, confirm_paid=True, request_id='once') == first
    assert generation.submit(pid, 1, 2, confirm_paid=True, request_id='new-click') == first
    with pytest.raises(ValueError, match='another shot'):
        generation.submit(pid, 2, 2, confirm_paid=True, request_id='once')
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        db.execute("UPDATE jobs SET status='SUCCEEDED' WHERE id=?", (first['id'],))
        # Retrying an accepted request must not revalidate changed inputs.
        db.execute("UPDATE shots SET duration=3 WHERE project_id=? AND seq=1", (pid,))
    replay = generation.submit(pid, 1, 2, confirm_paid=True, request_id='once')
    assert replay['id'] == first['id']
    assert replay['status'] == 'COMPLETED'


def test_reused_request_id_stays_idempotent_after_completion(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    first = generation.submit(pid, 1, 2, confirm_paid=True, request_id='original')
    duplicate = generation.submit(pid, 1, 2, confirm_paid=True, request_id='second-click')
    assert duplicate['id'] == first['id']
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='SUCCEEDED' WHERE id=?", (first['id'],))
    replay = generation.submit(pid, 1, 2, confirm_paid=True, request_id='second-click')
    assert replay['id'] == first['id']
    with pytest.raises(ValueError, match='another shot'):
        generation.submit(pid, 2, 2, confirm_paid=True, request_id='second-click')
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1


def test_mock_submit_still_completes_synchronously(project):
    pid, _ = project
    result = generation.submit(pid, 1, 0, confirm_paid=False, request_id='mock-sync')
    assert result['status'] == 'COMPLETED'
    assert Path(result['file']).read_bytes() == b'generated'
    assert result['asset_id']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_paid_job_asset_matches_boundary_provenance(boundary_project, monkeypatch):
    from studio import jobs, worker
    from studio.handlers import video
    from video_provider_core import CostEstimate, RemoteJob, RemoteStatus
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('UPDATE projects SET budget=5 WHERE id=?', (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', state['revision'])
    state = confirm_pair(pid, workflow.get(pid), 0, 1)
    result = generation.submit(pid, 1, 1, confirm_paid=True, request_id='provenance')
    job = jobs.get(result['id'])
    class Provider:
        def __init__(self, client): pass
        def estimate_cost(self, request):
            return CostEstimate(800_000, 'test:v1', 'test')
        def submit_video(self, request, key):
            assert request.first_frame and request.last_frame
            return RemoteJob('minimax', 'known')
        def status(self, remote, *, model=None):
            return RemoteStatus('succeeded', url='https://example.test/video')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    monkeypatch.setitem(worker.HANDLERS, 'video', video)
    monkeypatch.setattr(video, 'download', lambda client, url, target: target.write_bytes(b'completed'))
    monkeypatch.setattr(core, 'verify_video', lambda target, duration: None)
    assert worker.run_once() == job['id']
    # /assets can race /attempts in React: completion must already register output.
    assert len(timeline.assets(pid, 1)) == 1
    result = generation.attempt(pid, result['id'])
    timeline.select(pid, 1, result['asset_id'])
    shot = workflow.get(pid)['shots'][0]
    assert shot['old_video_decision_required'] is False
    assert shot['video_provenance']['frame_id'] == state['boundaries'][0]['confirmed_image_id']
    with core.connect() as db:
        db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL)', (pid, shot['id']))
    assert workflow.get(pid)['shots'][0]['old_video_decision_required'] is False
    with core.connect() as db:
        assert db.execute('SELECT 1 FROM guided_stale_shots WHERE project_id=? AND shot_id=?',
                          (pid, shot['id'])).fetchone() is None
    new_version = add_version(pid, state['boundaries'][0]['id'], 'new', size='1536x864')
    boundaries.confirm(pid, state['boundaries'][0]['id'], new_version, workflow.get(pid)['revision'])
    assert workflow.get(pid)['shots'][0]['old_video_decision_required'] is True


def test_guided_job_success_survives_reconcile_failure(boundary_project, monkeypatch):
    from studio import guided_compat, jobs, worker
    from studio.handlers import video
    from video_provider_core import CostEstimate, RemoteJob, RemoteStatus
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('UPDATE projects SET budget=5 WHERE id=?', (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', state['revision'])
    confirm_pair(pid, workflow.get(pid), 0, 1)
    result = generation.submit(pid, 1, 1, confirm_paid=True, request_id='reconcile-fail')
    class Provider:
        def __init__(self, client): pass
        def estimate_cost(self, request):
            return CostEstimate(800_000, 'test:v1', 'test')
        def submit_video(self, request, key):
            return RemoteJob('minimax', 'known')
        def status(self, remote, *, model=None):
            return RemoteStatus('succeeded', url='https://example.test/video')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    monkeypatch.setitem(worker.HANDLERS, 'video', video)
    monkeypatch.setattr(video, 'download', lambda client, url, target: target.write_bytes(b'completed'))
    monkeypatch.setattr(core, 'verify_video', lambda target, duration: None)
    monkeypatch.setattr(guided_compat, 'reconcile_outputs',
                        lambda pid: (_ for _ in ()).throw(RuntimeError('asset registry unavailable')))
    assert worker.run_once() == result['id']
    assert jobs.get(result['id'])['status'] == 'SUCCEEDED'
    with core.connect() as db:
        event = db.execute("SELECT data_json FROM job_events WHERE job_id=? AND type='guided_reconcile_failed'",
                           (result['id'],)).fetchone()
    assert 'asset registry unavailable' in event['data_json']


def test_worker_rejects_changed_confirmed_boundary(boundary_project, monkeypatch):
    from studio import jobs
    from studio.handlers import video
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('UPDATE projects SET budget=5 WHERE id=?', (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', state['revision'])
    confirm_pair(pid, workflow.get(pid), 0, 1)
    result = generation.submit(pid, 1, 1, confirm_paid=True, request_id='immutable')
    job = jobs.get(result['id'])
    payload = json.loads(job['payload_json'])
    Path(payload['first_frame_path']).write_bytes(b'replaced since authorization')
    with pytest.raises(ValueError, match='boundary.*changed'):
        video._request(job, payload, core.HOME / pid)


def test_minimax_rejects_undersized_boundary_frame(boundary_project, monkeypatch):
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    pid, state = boundary_project
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', workflow.get(pid)['revision'])
    small_id = add_version(pid, state['boundaries'][0]['id'], 'small', size='192x108')
    last_id = add_version(pid, state['boundaries'][1]['id'], 'last', size='1536x864')
    with core.connect() as db:
        # boundaries.confirm enforces a 256px minimum, so set the confirmed frame
        # directly to exercise preview's MiniMax dimension guard.
        db.execute('UPDATE guided_boundaries SET confirmed_image_id=? WHERE id=?',
                   (small_id, state['boundaries'][0]['id']))
        db.execute('UPDATE guided_boundaries SET confirmed_image_id=? WHERE id=?',
                   (last_id, state['boundaries'][1]['id']))
    with pytest.raises(ValueError, match='dimensions'):
        generation.preview(pid, 1)


def _pending_attempt(pid, tmp_path):
    directory = tmp_path / pid / 'guided-attempts' / ('a' * 32)
    directory.mkdir(parents=True)
    source = directory / ('b' * 32 + '.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=24',
                    '-t', '8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    with core.connect() as db:
        db.execute("UPDATE shots SET duration=6 WHERE project_id=? AND seq=1", (pid,))
        shot_id = db.execute('SELECT id FROM shots WHERE project_id=? AND seq=1', (pid,)).fetchone()[0]
        db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status,remote_id,video_url,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('a'*32, pid, shot_id, 'pending', 'prompt', 10, 'minimax', 1.2, 'DOWNLOAD_PENDING', 'remote', 'https://example.com/video', 'duration mismatch'))
    return source


def test_trim_pending_attempt_to_current_shot_duration_without_overwriting(project):
    pid, tmp_path = project
    source = _pending_attempt(pid, tmp_path)
    before = source.read_bytes()
    result = generation.trim_pending(pid, 'a'*32)
    assert result['status'] == 'COMPLETED'
    assert result['reserved_usd'] == 1.2
    assert source.read_bytes() == before
    assert Path(result['file']) != source
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                              '-of', 'default=nw=1:nk=1', result['file']], text=True))
    assert abs(duration - 6) < 0.25
    assert result['asset_id'] in [a['id'] for a in timeline.assets(pid, 1)]


def test_abandon_finished_remote_result_keeps_reservation_and_allows_new_attempt(project):
    pid, tmp_path = project
    source = _pending_attempt(pid, tmp_path)
    with pytest.raises(ValueError):
        generation.abandon_pending(pid, 'a'*32, confirm_checked=False)
    result = generation.abandon_pending(pid, 'a'*32, confirm_checked=True)
    assert result['status'] == 'ABANDONED'
    assert result['reserved_usd'] == 1.2
    assert result['video_url'] == 'https://example.com/video'
    assert source.exists()
    assert generation.submit(pid, 1, 0, confirm_paid=False, request_id='new')['status'] == 'COMPLETED'
    generation._update(pid, 'a'*32, status='DOWNLOAD_PENDING')
    assert generation.attempt(pid, 'a'*32)['status'] == 'ABANDONED'


def test_trim_rejects_video_shorter_than_current_shot(project):
    pid, tmp_path = project
    _pending_attempt(pid, tmp_path)
    with core.connect() as db:
        db.execute('UPDATE shots SET duration=9 WHERE project_id=? AND seq=1', (pid,))
    with pytest.raises(ValueError, match='shorter'):
        generation.trim_pending(pid, 'a'*32)
    assert generation.attempt(pid, 'a'*32)['status'] == 'DOWNLOAD_PENDING'
    assert timeline.assets(pid, 1) == []


def _paid_attempt(project, monkeypatch, status='UNKNOWN_REMOTE_STATE'):
    from studio import attempts, jobs
    pid, _ = project
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    queued = generation.submit(pid, 1, 2, confirm_paid=True, request_id='paid-action')
    job = jobs.get(queued['id'])
    attempt = attempts.create(job['id'], provider='minimax', model='MiniMax-H3',
                              input_data=json.loads(job['payload_json']))
    attempts.prepare_submission(attempt['id'], amount_microusd=1_000_000, pricing_version='test:v1')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='BLOCKED',block_reason='unknown_remote' WHERE id=?", (job['id'],))
        db.execute('UPDATE job_attempts SET status=?,remote_id=? WHERE id=?',
                   (status, None if status == 'UNKNOWN_REMOTE_STATE' else 'known', attempt['id']))
    return jobs.get(job['id']), attempts.get(attempt['id'])


@pytest.mark.parametrize('attach,known_unknown', [(True, False), (False, False), (False, True)])
def test_recover_never_submits(project, monkeypatch, attach, known_unknown):
    from studio import attempts, jobs
    from studio.handlers import video
    from video_provider_core import RemoteStatus
    job, original = _paid_attempt(project, monkeypatch, 'UNKNOWN_REMOTE_STATE' if attach else 'SUBMITTED')
    if known_unknown:
        attempts.mark_unknown(original['id'], 'query timeout')
    calls = {'submit': 0, 'query': 0}
    class Provider:
        def __init__(self, client): pass
        def submit_video(self, *args):
            calls['submit'] += 1
            raise AssertionError('Recovery must not submit')
        def status(self, remote, *, model=None):
            calls['query'] += 1
            assert remote.remote_id == 'known'
            return RemoteStatus('running' if calls['query'] == 1 else 'succeeded',
                                url='https://example.test/video')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    monkeypatch.setattr(video, 'download', lambda client, url, target: target.write_bytes(b'downloaded'))
    first = generation.recover(job['project_id'], job['id'],
                               'known' if attach else None, confirm_checked=True)
    assert first['status'] == 'SUBMITTED'
    assert calls == {'submit': 0, 'query': 1}
    result = generation.recover(job['project_id'], original['id'], confirm_checked=True)
    assert result['status'] == 'COMPLETED'
    assert result['asset_id']
    assert result['reserved_usd'] == 1
    assert Path(result['file']).read_bytes() == b'downloaded'
    assert calls == {'submit': 0, 'query': 2}
    assert len(attempts.list_for_job(job['id'])) == 1
    assert jobs.get(job['id'])['status'] == 'SUCCEEDED'
    assert generation.recover(job['project_id'], original['id'], confirm_checked=True) == result


def test_recover_requires_remote_and_respects_active_lease(project, monkeypatch):
    from studio import jobs
    job, original = _paid_attempt(project, monkeypatch)
    with pytest.raises(ValueError, match='Remote'):
        generation.recover(job['project_id'], original['id'], confirm_checked=True)
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='RUNNING',lease_owner='other',lease_expires='2999-01-01T00:00:00+00:00' WHERE id=?",
                   (job['id'],))
    with pytest.raises(ValueError, match='worker'):
        generation.recover(job['project_id'], original['id'], 'known', confirm_checked=True)
    assert jobs.latest_attempt(job['id'])['remote_id'] is None


@pytest.mark.parametrize('status', ['UNKNOWN_REMOTE_STATE', 'DOWNLOADING', 'VALIDATING'])
def test_abandon_unknown_attempt_keeps_reservation(project, monkeypatch, status):
    from studio import jobs
    job, original = _paid_attempt(project, monkeypatch, status)
    with core.connect() as db:
        costs = [dict(row) for row in db.execute('SELECT * FROM cost_entries')]
    result = generation.abandon_pending(job['project_id'], original['id'], confirm_checked=True)
    assert result['status'] == 'ABANDONED'
    assert result['reserved_usd'] == 1
    assert jobs.get(job['id'])['status'] == 'CANCELLED'
    assert generation.abandon_pending(job['project_id'], original['id'], confirm_checked=True) == result
    with core.connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM cost_entries')] == costs
    new = generation.submit(job['project_id'], 1, 2, confirm_paid=True, request_id='after-abandon')
    assert new['id'] != job['id']
    assert new['status'] == 'QUEUED'


def test_trim_preserves_original_and_reservation(project, monkeypatch):
    import hashlib
    from studio import attempts, jobs
    job, original = _paid_attempt(project, monkeypatch, 'COMPLETED')
    pid = job['project_id']
    source = core.HOME / pid / 'original.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=24',
                    '-t', '8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    output = json.dumps({'file': str(source), 'remote_id': 'known'})
    with core.connect() as db:
        db.execute('UPDATE shots SET duration=6 WHERE project_id=? AND seq=1', (pid,))
        db.execute("UPDATE jobs SET status='SUCCEEDED',output_json=? WHERE id=?", (output, job['id']))
        db.execute('UPDATE job_attempts SET output_json=? WHERE id=?', (output, original['id']))
        costs = [dict(row) for row in db.execute('SELECT * FROM cost_entries')]
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = generation.trim_pending(pid, original['id'], confirm_checked=True)
    assert result['status'] == 'COMPLETED'
    assert result['file'] != str(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert attempts.get(original['id'])['output_json'] == output
    assert result['asset_id'] in [asset['id'] for asset in timeline.assets(pid, 1)]
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                            '-of', 'default=nw=1:nk=1', result['file']], text=True))
    assert abs(duration - 6) < 0.25
    assert generation.trim_pending(pid, job['id'], confirm_checked=True) == result
    with core.connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM cost_entries')] == costs
    assert json.loads(jobs.get(job['id'])['output_json'])['original_file'] == str(source)


def test_trim_recovers_blocked_download(project, monkeypatch):
    import hashlib
    from studio import attempts, jobs, worker
    from studio.handlers import video
    from video_provider_core import CostEstimate, RemoteJob, RemoteStatus
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
        db.execute('UPDATE shots SET duration=6 WHERE project_id=? AND seq=1', (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    queued = generation.submit(pid, 1, 2, confirm_paid=True, request_id='long-download')
    job = jobs.get(queued['id'])
    class Provider:
        def __init__(self, client): pass
        def estimate_cost(self, request):
            return CostEstimate(1_000_000, 'test:v1', 'test')
        def submit_video(self, request, key):
            return RemoteJob('minimax', 'known')
        def status(self, remote, *, model=None):
            return RemoteStatus('succeeded', url='https://example.test/video')
    monkeypatch.setitem(video.PROVIDERS, 'minimax', Provider)
    monkeypatch.setitem(worker.HANDLERS, 'video', video)
    def download(client, url, target):
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=24',
                        '-t', '8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(target)], check=True)
    monkeypatch.setattr(video, 'download', download)
    def verify(path, expected):
        actual = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                                'format=duration', '-of', 'default=nw=1:nk=1',
                                                str(path)], text=True))
        if abs(actual - expected) > 0.25:
            raise RuntimeError(f'Invalid clip duration: expected {expected}s, got {actual:.3f}s')
    monkeypatch.setattr(core, 'verify_video', verify)
    assert worker.run_once() == job['id']
    blocked = jobs.get(job['id'])
    current = attempts.list_for_job(job['id'])[0]
    assert blocked['status'] == 'BLOCKED' and blocked['block_reason'] == 'download_failed'
    assert current['status'] == 'VALIDATING'
    with core.connect() as db:
        event = db.execute("SELECT data_json FROM job_events WHERE job_id=? AND type='download_ready'",
                           (job['id'],)).fetchone()
        costs = [dict(row) for row in db.execute('SELECT * FROM cost_entries')]
    source = Path(json.loads(event['data_json'])['file'])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    projected = generation.attempt(pid, job['id'])
    assert projected['status'] == 'DOWNLOAD_PENDING'
    assert projected['error'] == 'download_failed'
    trimmed = generation.trim_pending(pid, current['id'], confirm_checked=True)
    assert trimmed['status'] == 'COMPLETED'
    assert trimmed['file'] != str(source)
    assert jobs.get(job['id'])['status'] == 'SUCCEEDED'
    assert attempts.get(current['id'])['status'] == 'COMPLETED'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert trimmed['asset_id'] in [asset['id'] for asset in timeline.assets(pid, 1)]
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                            '-of', 'default=nw=1:nk=1', trimmed['file']], text=True))
    assert abs(duration - 6) < 0.25
    with core.connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM cost_entries')] == costs


def test_legacy_unknown_attempt_can_be_abandoned(project):
    pid, _ = project
    _pending_attempt(pid, core.HOME)
    with core.connect() as db:
        db.execute("UPDATE guided_attempts SET status='SUBMITTING',video_url=NULL WHERE id=?",
                   ('a'*32,))
    result = generation.abandon_pending(pid, 'a'*32, confirm_checked=True)
    assert result['status'] == 'ABANDONED'
    assert result['reserved_usd'] == 1.2
    assert generation.abandon_pending(pid, 'a'*32, confirm_checked=True) == result


def test_job_actions_require_explicit_confirmation(project, monkeypatch):
    job, original = _paid_attempt(project, monkeypatch)
    for action in (generation.recover, generation.trim_pending, generation.abandon_pending):
        with pytest.raises(ValueError, match='confirmation'):
            action(job['project_id'], original['id'], confirm_checked=False)


def test_job_and_legacy_attempt_actions_both_work(project, monkeypatch):
    job, original = _paid_attempt(project, monkeypatch)
    _pending_attempt(*project)
    pid = job['project_id']
    assert generation.trim_pending(pid, 'a'*32)['status'] == 'COMPLETED'
    assert generation.abandon_pending(pid, original['id'], confirm_checked=True)['status'] == 'ABANDONED'
