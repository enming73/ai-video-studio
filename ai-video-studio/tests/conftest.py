import pytest

from studio import core, guided_workflow as workflow, guided_characters


def boundary_draft():
    return {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks through the forest.', 'duration': 8, 'character_ids': []},
        {'prompt': 'An elephant reaches the river.', 'duration': 12, 'character_ids': []}]}


@pytest.fixture
def boundary_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = guided_characters.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, boundary_draft(), state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    return pid, state
