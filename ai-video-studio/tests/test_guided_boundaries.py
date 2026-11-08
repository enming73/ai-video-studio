from studio import core, guided_boundaries as boundaries, guided_workflow as workflow


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
