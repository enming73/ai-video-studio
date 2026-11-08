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


def test_derived_prompts_request_one_frame_and_one_moment(boundary_project):
    pid, state = boundary_project
    first, middle, last = state['boundaries']
    with core.connect() as db:
        opening = boundaries.derive(db, pid, first['id'])['prompt']
        transition = boundaries.derive(db, pid, middle['id'])['prompt']
        closing = boundaries.derive(db, pid, last['id'])['prompt']
    for prompt in (opening, transition, closing):
        lower = prompt.lower()
        assert 'single cinematic still photograph' in lower
        assert 'do not create a storyboard' in lower
        assert 'collage' in lower
        assert 'multiple panels' in lower
        assert 'one unified scene' in lower
        assert 'character appears only once' in lower
    assert 'An elephant walks through the forest.' in opening
    assert 'An elephant walks through the forest.' in transition
    assert 'An elephant reaches the river.' in transition
    assert 'An elephant reaches the river.' in closing


def test_derived_transition_uses_only_the_cut_moments(boundary_project):
    pid, state = boundary_project
    filler = 'FILLER. ' * 80
    with core.connect() as db:
        db.execute('UPDATE shots SET prompt=? WHERE project_id=? AND seq=1',
                   ('OPENING_SENTENCE. ' + filler + 'TAIL_SENTENCE.', pid))
        db.execute('UPDATE shots SET prompt=? WHERE project_id=? AND seq=2',
                   ('HEAD_SENTENCE. ' + filler + 'FINAL_SENTENCE.', pid))
        prompt = boundaries.derive(db, pid, state['boundaries'][1]['id'])['prompt']
    assert 'TAIL_SENTENCE.' in prompt
    assert 'HEAD_SENTENCE.' in prompt
    assert 'OPENING_SENTENCE.' not in prompt
    assert 'FINAL_SENTENCE.' not in prompt
    assert len(prompt) < 1200


def test_excerpt_keeps_visual_beats_and_drops_camera_noise():
    assert '陶渊明端起陶杯' in boundaries._excerpt('镜头缓慢环绕人物。陶渊明端起陶杯。', 'start')
    assert '镜头缓慢环绕' not in boundaries._excerpt('镜头缓慢环绕人物。陶渊明端起陶杯。', 'start')
    assert '陶渊明端起陶杯' in boundaries._excerpt('镜头缓慢环绕人物。陶渊明端起陶杯。', 'end')
    assert 'Camera pans' not in boundaries._excerpt('Luma walks. Camera pans left.', 'end')
    assert 'Luma walks' in boundaries._excerpt('Luma walks. Camera pans left.', 'end')


def test_excerpt_joins_english_sentences_with_a_space():
    assert boundaries._excerpt('Luma walks. The tree is big.', 'start') == 'Luma walks. The tree is big.'


def test_excerpt_joins_chinese_sentences_without_extra_spaces():
    assert boundaries._excerpt('陶渊明端起陶杯。他望向远方。', 'start') == '陶渊明端起陶杯。他望向远方。'


def test_excerpt_does_not_drop_words_that_only_look_like_camera_noise():
    assert 'Pandora opens the box.' in boundaries._excerpt('Pandora opens the box. Luma walks.', 'start')
    assert 'Faded memories return.' in boundaries._excerpt('Faded memories return. Luma walks.', 'start')


def test_excerpt_does_not_split_decimals():
    assert '3.5 meters.' in boundaries._excerpt('The tree is 3.5 meters. The river is wide.', 'start')


def test_excerpt_falls_back_when_every_sentence_is_camera_noise():
    assert boundaries._excerpt('Camera pans. Cut to a close-up.', 'start')


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

