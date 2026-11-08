import pytest

from studio import core, guided_workflow as workflow, timeline, sound


@pytest.fixture
def guided(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A small elephant in the forest', 20, 'mock', 3)
    workflow.create(pid)
    return pid, tmp_path


def draft():
    return {'character_bible': {}, 'shots': [
        {'prompt': 'The elephant walks in the forest.', 'duration': 8},
        {'prompt': 'The elephant approaches a river.', 'duration': 12}]}


def test_draft_persists_and_rejects_stale_confirmation(guided):
    pid, _ = guided
    state = workflow.save_draft(pid, draft(), 0)
    assert workflow.get(pid)['draft'] == draft()
    with pytest.raises(ValueError):
        workflow.confirm_storyboard(pid, 0)
    state = workflow.confirm_storyboard(pid, state['revision'])
    assert state['storyboard_confirmed']
    assert [s['duration'] for s in core.storyboard(pid)['shots']] == [8, 12]


def test_confirm_shots_in_any_order_invalidate_edited_shot_only(guided):
    pid, root = guided
    state = workflow.save_draft(pid, draft(), 0)
    state = workflow.confirm_storyboard(pid, state['revision'])
    paths = []
    for seq in (1, 2):
        path = root / f'{seq}.mp4'
        path.write_bytes(b'media')
        paths.append(timeline.import_asset(pid, seq, str(path)))
        timeline.select(pid, seq, paths[-1])
    state = workflow.get(pid)
    state = workflow.confirm_shot(pid, 2, paths[1], state['revision'])
    assert state['shots'][1]['confirmed'] and not state['shots'][0]['confirmed']
    state = workflow.confirm_shot(pid, 1, paths[0], state['revision'])
    assert workflow.render_ready(pid)
    core.update_shot(pid, 1, 'The elephant walks slowly through the forest.')
    state = workflow.get(pid)
    assert not state['shots'][0]['confirmed']
    assert state['shots'][1]['confirmed']
    assert not workflow.render_ready(pid)


def test_invalid_draft_does_not_create_shots(guided):
    pid, _ = guided
    value = draft()
    value['shots'][0]['duration'] = 7
    state = workflow.save_draft(pid, value, 0)
    with pytest.raises(ValueError):
        workflow.confirm_storyboard(pid, state['revision'])
    assert core.storyboard(pid)['shots'] == []


def test_selecting_another_version_invalidates_only_its_confirmation(guided):
    pid, root = guided
    state = workflow.save_draft(pid, draft(), 0)
    state = workflow.confirm_storyboard(pid, state['revision'])
    for seq in (1, 2):
        path = root / f'old-{seq}.mp4'
        path.write_bytes(f'old{seq}'.encode())
        asset = timeline.import_asset(pid, seq, str(path))
        timeline.select(pid, seq, asset)
        state = workflow.get(pid)
        workflow.confirm_shot(pid, seq, asset, state['revision'])
    replacement = root / 'new.mp4'
    replacement.write_bytes(b'new')
    aid = timeline.import_asset(pid, 1, str(replacement))
    timeline.select(pid, 1, aid)
    state = workflow.get(pid)
    assert [s['confirmed'] for s in state['shots']] == [False, True]
    assert not workflow.render_ready(pid)


def test_existing_project_is_not_converted(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Existing project', 20, 'mock', 0)
    core.plan(pid, 10)
    with pytest.raises(ValueError):
        workflow.create(pid)
    assert len(core.storyboard(pid)['shots']) == 2


def test_guided_project_cannot_bypass_gating_via_legacy_functions(guided, monkeypatch):
    pid, _ = guided
    state = workflow.save_draft(pid, draft(), 0)
    workflow.confirm_storyboard(pid, state['revision'])
    monkeypatch.setattr(core, 'require_ffmpeg', lambda: None)
    monkeypatch.setattr(core, 'mock_generate', lambda shot, path: path.write_bytes(b'video'))
    with pytest.raises(ValueError):
        core.generate(pid)
    with pytest.raises(ValueError):
        core.render(pid)
    with pytest.raises(ValueError):
        timeline.render(pid)


def test_external_video_cannot_bypass_guided_render_gate(guided, monkeypatch):
    pid, root = guided
    state = workflow.save_draft(pid, draft(), 0)
    workflow.confirm_storyboard(pid, state['revision'])
    video = root / 'external.mp4'
    video.write_bytes(b'video')
    monkeypatch.setattr(sound, '_duration', lambda pid: 20)
    monkeypatch.setattr(sound, 'config', lambda pid: {})
    monkeypatch.setattr(sound, '_probe', lambda path: 20)
    with pytest.raises(ValueError, match='guided'):
        sound.export(pid, video=str(video))
