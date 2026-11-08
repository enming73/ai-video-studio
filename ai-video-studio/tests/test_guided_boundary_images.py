import base64
from pathlib import Path

from studio import core, guided_boundaries as boundaries, guided_images as images, guided_workflow as workflow
from tests.helpers import add_version, attach_video, make_png


def test_preview_does_not_require_earlier_shots(boundary_project):
    pid, state = boundary_project
    details = boundaries.preview(pid, state['shots'][1]['start_boundary_id'], 'gpt-image-2.5-sunburst')
    assert details['position'] == 1
    assert 'End of shot 1' in details['prompt']


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
    new_id = add_version(pid, middle['id'], 'middle')
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
