import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import core, guided_boundaries as boundaries, guided_images as images
from studio import guided_workflow as workflow, timeline, webapp


def make_video(path, size='1280x720', duration=1):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                    '-i', f'testsrc2=size={size}:rate=24:duration={duration}',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


def attach(pid, seq, path):
    asset = timeline.import_asset(pid, seq, str(path))
    timeline.select(pid, seq, asset)
    return asset


def test_extract_end_creates_unconfirmed_candidate(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))
    state = workflow.get(pid)
    end = state['shots'][0]['end_boundary_id']
    result = boundaries.extract_from_asset(pid, 1, 'end', state['revision'])
    assert result['boundary_id'] == end
    assert result['revision'] == state['revision'] + 1
    candidates = boundaries.list_frames(pid, end)
    assert len(candidates) == 1
    assert not candidates[0]['confirmed']
    assert candidates[0]['stale'] == 0
    assert images.image_dimensions(images.image_path(pid, candidates[0]['id'])) == (1536, 864)


def test_extract_start_creates_candidate_on_start_boundary(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 2, make_video(tmp_path / 'shot-2.mp4'))
    state = workflow.get(pid)
    start = state['shots'][1]['start_boundary_id']
    result = boundaries.extract_from_asset(pid, 2, 'start', state['revision'])
    assert result['boundary_id'] == start
    assert workflow.get(pid)['boundaries'][1]['candidate_count'] == 1


def test_extract_first_and_last_frames_are_different(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))
    state = workflow.get(pid)
    start = boundaries.extract_from_asset(pid, 1, 'start', state['revision'])
    state = workflow.get(pid)
    end = boundaries.extract_from_asset(pid, 1, 'end', state['revision'])
    assert images.image_path(pid, start['id']).read_bytes() != images.image_path(pid, end['id']).read_bytes()


def test_extract_crops_non_16_9_source_to_1536x864(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'tall.mp4', size='640x480'))
    state = workflow.get(pid)
    result = boundaries.extract_from_asset(pid, 1, 'end', state['revision'])
    assert images.image_dimensions(images.image_path(pid, result['id'])) == (1536, 864)


def test_extract_requires_selected_asset(boundary_project):
    pid, state = boundary_project
    with pytest.raises(ValueError, match='asset'):
        boundaries.extract_from_asset(pid, 1, 'end', workflow.get(pid)['revision'])


def test_extract_rejects_changed_asset(boundary_project, tmp_path):
    pid, state = boundary_project
    path = make_video(tmp_path / 'shot-1.mp4')
    attach(pid, 1, path)
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='changed'):
        boundaries.extract_from_asset(pid, 1, 'end', workflow.get(pid)['revision'])


def test_extract_deduplicates_identical_frame(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))
    state = workflow.get(pid)
    first = boundaries.extract_from_asset(pid, 1, 'end', state['revision'])
    second = boundaries.extract_from_asset(pid, 1, 'end', workflow.get(pid)['revision'])
    assert second['id'] == first['id']
    assert len(boundaries.list_frames(pid, first['boundary_id'])) == 1


def test_extract_rejects_unknown_side(boundary_project, tmp_path):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))
    with pytest.raises(ValueError, match='side'):
        boundaries.extract_from_asset(pid, 1, 'middle', workflow.get(pid)['revision'])


def test_extract_single_frame_video_uses_same_frame_for_both_sides(boundary_project, tmp_path):
    pid, state = boundary_project
    path = tmp_path / 'single.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                    '-i', 'testsrc2=size=1280x720:rate=24', '-frames:v', '1',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    attach(pid, 1, path)
    start = boundaries.extract_from_asset(pid, 1, 'start', workflow.get(pid)['revision'])
    end = boundaries.extract_from_asset(pid, 1, 'end', workflow.get(pid)['revision'])
    assert images.image_path(pid, start['id']).read_bytes() == images.image_path(pid, end['id']).read_bytes()


def test_extract_translates_missing_binary_to_value_error(boundary_project, tmp_path, monkeypatch):
    pid, state = boundary_project
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))

    def missing(*args, **kwargs):
        raise FileNotFoundError('ffprobe')

    monkeypatch.setattr(subprocess, 'run', missing)
    with pytest.raises(ValueError, match='FFmpeg|FFprobe'):
        boundaries.extract_from_asset(pid, 1, 'end', workflow.get(pid)['revision'])


def test_extract_route_and_validation(boundary_project, tmp_path):
    pid, state = boundary_project
    api = TestClient(webapp.app)
    base = f'/api/projects/{pid}/guided'
    end = workflow.get(pid)['shots'][0]['end_boundary_id']
    response = api.post(base + '/shots/1/extract-boundary', json={'side': 'end', 'revision': state['revision']})
    assert response.status_code == 400
    attach(pid, 1, make_video(tmp_path / 'shot-1.mp4'))
    response = api.post(base + '/shots/1/extract-boundary',
                        json={'side': 'end', 'revision': workflow.get(pid)['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['boundary_id'] == end
    assert api.post(base + '/shots/1/extract-boundary',
                    json={'side': 'middle', 'revision': workflow.get(pid)['revision']}).status_code == 422
