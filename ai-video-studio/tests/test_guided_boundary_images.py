import base64
from pathlib import Path

import pytest

from studio import core, guided_boundaries as boundaries, guided_images as images, guided_workflow as workflow
from tests.helpers import add_version, attach_video, make_png


def test_preview_does_not_require_earlier_shots(boundary_project):
    pid, state = boundary_project
    details = boundaries.preview(pid, state['shots'][1]['start_boundary_id'], 'gpt-image-2.5-sunburst')
    assert details['position'] == 1
    assert 'The outgoing scene is ending' in details['prompt']


def test_generated_frame_is_cropped_to_16_9(boundary_project, monkeypatch):
    pid, state = boundary_project
    raw = make_png(Path(core.HOME) / 'raw.png')
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    encoded = {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
    monkeypatch.setattr(images, '_image_request', lambda *a, **k: encoded)
    monkeypatch.setattr(images, '_image_edit', lambda *a, **k: encoded)
    attempt = boundaries.generate(pid, state['boundaries'][0]['id'], 'gpt-image-2.5-sunburst', 1,
                                  confirm_paid=True, request_id='frame-1', revision=state['revision'])
    assert attempt['status'] == 'COMPLETED'
    assert images.image_dimensions(images.image_path(pid, attempt['image_id'])) == (1536, 864)


def test_confirm_and_unconfirm_cascade_to_both_neighbours(boundary_project):
    pid, state = boundary_project
    first, middle, last = state['boundaries']
    shot_a, shot_b = state['shots']
    asset_a = attach_video(pid, shot_a, first['id'], middle['id'])
    asset_b = attach_video(pid, shot_b, middle['id'], last['id'])
    new_id = add_version(pid, middle['id'], 'middle', size='1536x864')
    current = workflow.get(pid)['revision']
    boundaries.confirm(pid, middle['id'], new_id, current)
    state = workflow.get(pid)
    assert [s['old_video_decision_required'] for s in state['shots']] == [True, True]
    assert asset_a.exists() and asset_b.exists()
    boundaries.unconfirm(pid, middle['id'], state['revision'])
    assert [s['old_video_decision_required'] for s in workflow.get(pid)['shots']] == [True, True]


def test_crop_creates_a_new_version_and_keeps_the_old(boundary_project):
    pid, state = boundary_project
    boundary_id = state['boundaries'][0]['id']
    original = add_version(pid, boundary_id, 'legacy-3x2')
    original_bytes = images.image_path(pid, original).read_bytes()
    result = boundaries.crop(pid, boundary_id, original, state['revision'])
    assert result['id'] != original
    assert images.image_dimensions(images.image_path(pid, result['id'])) == (1536, 864)
    assert images.image_path(pid, original).read_bytes() == original_bytes


def test_insert_version_assigns_fresh_ids_for_the_same_file(boundary_project):
    pid, state = boundary_project
    boundary_id = state['boundaries'][0]['id']
    path = Path(core.HOME) / pid / 'guided-images' / 'shared.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    make_png(path)
    with core.connect() as db:
        first = boundaries.insert_version(db, pid, boundary_id, path, stale=False, prompt='p', model='test', references=[])
        second = boundaries.insert_version(db, pid, boundary_id, path, stale=False, prompt='p', model='test', references=[])
    assert first != second
    with core.connect() as db:
        count = db.execute('SELECT COUNT(*) FROM guided_boundary_versions WHERE project_id=? AND boundary_id=?',
                           (pid, boundary_id)).fetchone()[0]
    assert count == 2
    assert images.image_path(pid, first).read_bytes() == images.image_path(pid, second).read_bytes()


def test_non_16_9_candidate_cannot_be_confirmed_until_cropped(boundary_project):
    pid, state = boundary_project
    boundary_id = state['boundaries'][0]['id']
    original = add_version(pid, boundary_id, 'wide', size='1536x1024')
    with pytest.raises(ValueError):
        boundaries.confirm(pid, boundary_id, original, state['revision'])
    cropped = boundaries.crop(pid, boundary_id, original, state['revision'])
    boundaries.confirm(pid, boundary_id, cropped['id'], workflow.get(pid)['revision'])
    assert workflow.get(pid)['boundaries'][0]['confirmed_image_id'] == cropped['id']
