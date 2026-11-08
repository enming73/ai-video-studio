import pytest

from studio import core, guided_boundaries as boundaries, guided_characters as roles
from studio import guided_images as images, guided_workflow as workflow
from tests.helpers import add_version, attach_video, make_png


@pytest.fixture
def three(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant drinks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant sleeps.', 'duration': 8, 'character_ids': []}]}, state['revision'])
    return pid, workflow.confirm_storyboard(pid, state['revision'])


def confirm_all(pid, state):
    for i, boundary in enumerate(state['boundaries']):
        with core.connect() as db:
            references = [(r['id'], r['image_id']) for r in boundaries.derive(db, pid, boundary['id'])['references']]
        version = add_version(pid, boundary['id'], f'version-{i}', size='1536x864', references=references)
        boundaries.confirm(pid, boundary['id'], version, workflow.get(pid)['revision'])
    return workflow.get(pid)


def test_boundary_change_marks_both_neighbours_but_not_others(three):
    pid, state = three
    b, shots = state['boundaries'], state['shots']
    attach_video(pid, shots[0], b[0]['id'], b[1]['id'])
    attach_video(pid, shots[1], b[1]['id'], b[2]['id'])
    attach_video(pid, shots[2], b[2]['id'], b[3]['id'])
    with core.connect() as db:
        db.execute('DELETE FROM guided_stale_shots WHERE project_id=?', (pid,))
    new_id = add_version(pid, b[1]['id'], 'new-b1', size='1536x864')
    boundaries.confirm(pid, b[1]['id'], new_id, workflow.get(pid)['revision'])
    assert [s['old_video_decision_required'] for s in workflow.get(pid)['shots']] == [True, True, False]


def test_prompt_edit_invalidates_both_adjacent_boundaries(three):
    pid, state = three
    confirm_all(pid, state)
    workflow.edit_shot(pid, 2, 'An elephant drinks slowly.', workflow.get(pid)['revision'])
    state = workflow.get(pid)
    assert [b['confirmed'] for b in state['boundaries']] == [True, False, False, True]


def test_cast_edit_invalidates_internal_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    cid = state['characters'][0]['id']
    raw = make_png(tmp_path / 'luma.png', '512x512')
    image = images.import_character_image(pid, cid, 'luma.png', raw, state['revision'])
    state = roles.confirm_character(pid, cid, image['id'], image['revision'])
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'Luma walks.', 'duration': 5, 'character_ids': [cid]},
        {'prompt': 'Luma drinks.', 'duration': 5, 'character_ids': [cid]}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    confirm_all(pid, state)
    roles.set_shot_characters(pid, 1, [], workflow.get(pid)['revision'])
    assert workflow.get(pid)['boundaries'][1]['confirmed'] is False


def test_character_image_change_only_affects_boundaries_that_used_it(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    cid = state['characters'][0]['id']
    first = images.import_character_image(pid, cid, 'luma.png', make_png(tmp_path / 'luma.png', '512x512'), state['revision'])
    state = roles.confirm_character(pid, cid, first['id'], first['revision'])
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'Luma walks.', 'duration': 5, 'character_ids': [cid]},
        {'prompt': 'The river is quiet.', 'duration': 5, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    confirm_all(pid, state)
    second = images.import_character_image(pid, cid, 'luma2.png', make_png(tmp_path / 'luma2.png', '512x512'),
                                           workflow.get(pid)['revision'])
    roles.confirm_character(pid, cid, second['id'], second['revision'])
    assert [b['confirmed'] for b in workflow.get(pid)['boundaries']] == [False, False, True]
