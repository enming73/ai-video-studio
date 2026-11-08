from pathlib import Path

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

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
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    calls = []
    class Provider:
        def __init__(self, client): pass
        def submit(self, prompt, duration):
            calls.append(prompt)
            raise OSError('network uncertain')
    monkeypatch.setitem(core.PROVIDERS, 'minimax', Provider)
    state = generation.submit(pid, 1, 2, confirm_paid=True, request_id='ambiguous')
    assert state['status'] == 'SUBMITTING'
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 2, confirm_paid=True, request_id='different')
    assert len(calls) == 1


def test_concurrent_duplicate_request_submits_only_once(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    entered, release = Event(), Event()
    calls = []
    class Provider:
        def __init__(self, client): pass
        def submit(self, prompt, duration):
            calls.append(prompt)
            entered.set()
            assert release.wait(5)
            return 'remote-1'
    monkeypatch.setitem(core.PROVIDERS, 'minimax', Provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(generation.submit, pid, 1, 2, confirm_paid=True, request_id='same')
        assert entered.wait(5)
        duplicate = pool.submit(generation.submit, pid, 1, 2, confirm_paid=True, request_id='same').result(timeout=5)
        assert duplicate['status'] == 'SUBMITTING'
        release.set()
        assert first.result(timeout=5)['status'] == 'SUBMITTED'
    assert len(calls) == 1


def test_mock_frame_video_produces_real_two_half_clip(boundary_project):
    pid, state = boundary_project
    generation.set_video_model(pid, 'mock', 'mock', workflow.get(pid)['revision'])
    state = confirm_pair(pid, state, 0, 1)
    attempt = generation.submit(pid, 1, 0, confirm_paid=False, request_id='real-ffmpeg')
    assert attempt['status'] == 'COMPLETED'
    path = Path(attempt['file'])
    assert path.is_file() and path.stat().st_size > 0
    core.verify_video(path, state['shots'][0]['duration'])


def test_submit_passes_both_boundaries_to_i2v(boundary_project, monkeypatch):
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('UPDATE projects SET budget=5 WHERE id=?', (pid,))
    generation.set_video_model(pid, 'minimax', 'MiniMax-H3', workflow.get(pid)['revision'])
    state = confirm_pair(pid, state, 0, 1)
    first_bytes = images.image_path(pid, state['boundaries'][0]['confirmed_image_id']).read_bytes()
    last_bytes = images.image_path(pid, state['boundaries'][1]['confirmed_image_id']).read_bytes()
    received = []
    class FakeProvider:
        def __init__(self, client): pass
        def submit_i2v(self, prompt, duration, first_frame, first_mime, last_frame, last_mime):
            received.append((prompt, duration, first_frame, first_mime, last_frame, last_mime))
            return 'remote-1'
    monkeypatch.setitem(core.PROVIDERS, 'minimax', FakeProvider)
    attempt = generation.submit(pid, 1, 1, confirm_paid=True, request_id='i2v')
    assert attempt['status'] == 'SUBMITTED'
    prompt, duration, first_frame, first_mime, last_frame, last_mime = received[0]
    assert first_frame == first_bytes
    assert last_frame == last_bytes
    assert first_mime == 'image/png'
    assert last_mime == 'image/png'
    assert duration == state['shots'][0]['duration']


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
