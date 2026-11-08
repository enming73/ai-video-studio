import hashlib

import pytest

from studio import core, guided_boundaries as boundaries, guided_workflow as workflow
from tests.helpers import attach_video, make_png


@pytest.fixture
def legacy_project(boundary_project):
    pid, state = boundary_project
    shots = state['shots']
    with core.connect() as db:
        db.execute('DELETE FROM guided_boundaries WHERE project_id=?', (pid,))
        directory = core.HOME / pid / 'guided-images'
        directory.mkdir(parents=True, exist_ok=True)
        for shot in shots:
            path = directory / f'legacy-{shot["seq"]}.png'
            raw = make_png(path, '1536x1024')
            frame_id = f'legacy-frame-{shot["seq"]}'
            db.execute('INSERT INTO guided_frame_versions VALUES (?,?,?,?,?,0)',
                       (frame_id, pid, shot['id'], str(path), hashlib.sha256(raw).hexdigest()))
            db.execute('INSERT INTO guided_frame_details VALUES (?,?,?,?)',
                       (frame_id, 'old prompt', 'old-model', core.now()))
            db.execute('INSERT INTO guided_frame_selection VALUES (?,?,?)', (pid, shot['id'], frame_id))
    attach_video(pid, shots[0], 'legacy-frame-1', 'legacy-frame-1')
    return pid, state


def test_backfill_is_idempotent_and_lossless(legacy_project):
    pid, state = legacy_project
    first = workflow.get(pid)
    second = workflow.get(pid)
    assert first['boundaries'] == second['boundaries']
    assert len(first['boundaries']) == 3
    assert first['boundaries'][0]['candidate_count'] == 1
    assert first['boundaries'][1]['candidate_count'] == 1
    assert first['boundaries'][2]['candidate_count'] == 0
    assert first['shots'][0]['old_video_decision_required'] is True
    assert (core.HOME / pid / 'guided-images' / 'legacy-1.png').exists()
    assert (core.HOME / 'video-1.mp4').exists()


def test_backfill_creates_empty_boundaries_without_legacy_frames(boundary_project):
    pid, state = boundary_project
    with core.connect() as db:
        db.execute('DELETE FROM guided_boundaries WHERE project_id=?', (pid,))
    first = workflow.get(pid)
    assert [b['position'] for b in first['boundaries']] == [0, 1, 2]
    assert all(b['candidate_count'] == 0 for b in first['boundaries'])
    assert all(not b['confirmed'] for b in first['boundaries'])
    second = workflow.get(pid)
    assert second['boundaries'] == first['boundaries']


def test_backfilled_3_2_candidate_cannot_be_confirmed_before_crop(legacy_project):
    pid, state = legacy_project
    state = workflow.get(pid)
    candidate = boundaries.list_frames(pid, state['boundaries'][0]['id'])[0]
    with pytest.raises(ValueError):
        boundaries.confirm(pid, state['boundaries'][0]['id'], candidate['id'], state['revision'])
    cropped = boundaries.crop(pid, state['boundaries'][0]['id'], candidate['id'], state['revision'])
    boundaries.confirm(pid, state['boundaries'][0]['id'], cropped['id'], workflow.get(pid)['revision'],
                       allow_stale=True)
