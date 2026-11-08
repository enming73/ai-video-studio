import pytest

from studio import core, guided_boundaries as boundaries, guided_characters as characters
from studio import guided_images as images, guided_workflow as workflow
from tests.helpers import make_png


@pytest.fixture
def character_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = characters.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    cid = state['characters'][0]['id']
    raw = make_png(tmp_path / 'luma.png', '512x512')
    image = images.import_character_image(pid, cid, 'luma.png', raw, state['revision'])
    state = characters.confirm_character(pid, cid, image['id'], image['revision'])
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'Luma walks.', 'duration': 5, 'character_ids': [cid]},
        {'prompt': 'Luma drinks.', 'duration': 5, 'character_ids': [cid]}]}, state['revision'])
    return pid, workflow.confirm_storyboard(pid, state['revision']), cid


def test_confirmed_storyboard_creates_n_plus_one_boundaries(boundary_project):
    pid, state = boundary_project
    assert [b['position'] for b in state['boundaries']] == [0, 1, 2]
    assert all(not b['confirmed'] for b in state['boundaries'])
    shots = state['shots']
    assert shots[0]['start_boundary_id'] == state['boundaries'][0]['id']
    assert shots[0]['end_boundary_id'] == state['boundaries'][1]['id']
    assert shots[1]['start_boundary_id'] == state['boundaries'][1]['id']


def test_derived_prompts(boundary_project):
    pid, state = boundary_project
    first, middle, last = state['boundaries']
    with core.connect() as db:
        assert boundaries.derive(db, pid, first['id'])['prompt'] == (
            'Create a cinematic opening frame for shot 1: An elephant walks through the forest.')
        assert boundaries.derive(db, pid, middle['id'])['prompt'] == (
            'Create a cinematic transition frame. End of shot 1: An elephant walks through the forest. '
            'Next: An elephant reaches the river.')
        assert boundaries.derive(db, pid, last['id'])['prompt'] == (
            'Create a cinematic closing frame for shot 2: An elephant reaches the river.')


def test_non_character_project_has_no_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Plain project', 10, 'mock', 0)
    workflow.create(pid)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A river flows in the valley.', 'duration': 10}]}, 0)
    state = workflow.confirm_storyboard(pid, state['revision'])
    assert state['boundaries'] is None


def test_preview_requires_confirmed_characters(character_project):
    pid, state, cid = character_project
    characters.save_character(pid, cid, 'Luma', 'A large gray elephant', state['revision'])
    boundary_id = workflow.get(pid)['boundaries'][0]['id']
    with pytest.raises(ValueError, match='characters'):
        boundaries.preview(pid, boundary_id, 'gpt-image-2.5-sunburst')


def test_preview_requires_adjacent_cast_review(character_project):
    pid, state, _cid = character_project
    with core.connect() as db:
        shot_id = workflow.get(pid)['shots'][0]['id']
        db.execute('DELETE FROM guided_shot_cast_reviewed WHERE project_id=? AND shot_id=?', (pid, shot_id))
    boundary_id = workflow.get(pid)['boundaries'][1]['id']
    with pytest.raises(ValueError, match='cast'):
        boundaries.preview(pid, boundary_id, 'gpt-image-2.5-sunburst')


def test_preview_succeeds_when_gates_satisfied(character_project):
    pid, _state, _cid = character_project
    boundary_id = workflow.get(pid)['boundaries'][1]['id']
    details = boundaries.preview(pid, boundary_id, 'gpt-image-2.5-sunburst')
    assert details['position'] == 1


def test_confirm_rejects_unknown_boundary_image_before_reading_file(boundary_project):
    pid, state = boundary_project
    with pytest.raises(ValueError, match='boundary image'):
        boundaries.confirm(pid, state['boundaries'][0]['id'], 'nonexistent-image', state['revision'])


def test_crop_rejects_unknown_boundary_image_before_reading_file(boundary_project):
    pid, state = boundary_project
    with pytest.raises(ValueError, match='boundary image'):
        boundaries.crop(pid, state['boundaries'][0]['id'], 'nonexistent-image', state['revision'])

