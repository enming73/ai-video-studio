import pytest

from studio import core, timeline, guided_workflow as workflow, guided_characters as characters


@pytest.fixture
def old_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A story in the forest', 20, 'mock', 2)
    workflow.create(pid)
    draft = {'character_bible': {'A': 'elephant'}, 'shots': [
        {'prompt': 'An elephant walks in the forest.', 'duration': 10},
        {'prompt': 'An elephant approaches the river.', 'duration': 10}]}
    state = workflow.save_draft(pid, draft, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    return pid, tmp_path


def test_activation_preserves_existing_storyboard_and_requires_reconfirmation(old_project):
    pid, root = old_project
    report = root / pid / 'director-reports' / 'original.json'
    report.parent.mkdir(parents=True)
    report.write_text('original report')
    before = core.storyboard(pid)
    revision = workflow.get(pid)['revision']
    result = characters.activate(pid, revision)
    assert result['enabled'] is True
    assert core.storyboard(pid) == before
    assert report.read_text() == 'original report'
    assert not result['storyboard_reconfirmed']
    assert characters.get(pid)['enabled'] is True
    with pytest.raises(ValueError, match='revision'):
        characters.save_character(pid, None, 'A', 'small elephant', revision)


def test_role_names_and_cross_project_ids_are_checked(old_project):
    pid, _ = old_project
    state = characters.activate(pid, workflow.get(pid)['revision'])
    state = characters.save_character(pid, None, 'Luma', 'Small gray elephant', state['revision'])
    with pytest.raises(ValueError):
        characters.save_character(pid, None, 'Luma', 'Another role', state['revision'])
    with pytest.raises(ValueError):
        characters.set_shot_characters(pid, 1, ['another-project-role'], state['revision'])


def test_existing_bible_can_be_imported_without_paid_llm(old_project, monkeypatch):
    pid, _ = old_project
    state = characters.activate(pid, workflow.get(pid)['revision'])
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    state = characters.import_existing_bible(pid, state['revision'])
    assert state['characters'][0]['name'] == 'A'
    assert state['characters'][0]['description'] == 'elephant'
    assert core.storyboard(pid)['character_bible'] == {'A': 'elephant'}


def test_activation_marks_existing_video_provenance_unknown(old_project):
    pid, root = old_project
    video = root / 'old.mp4'
    video.write_bytes(b'old')
    aid = timeline.import_asset(pid, 1, str(video))
    timeline.select(pid, 1, aid)
    state = characters.activate(pid, workflow.get(pid)['revision'])
    shot = workflow.get(pid)['shots'][0]
    assert state['enabled']
    assert shot['old_video_decision_required']
    assert video.read_bytes() == b'old'


def test_import_existing_bible_keeps_draft_character_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest story', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    draft = {'character_bible': {'Luma': 'elephant'},
             'characters': [{'id': 'draft-role', 'name': 'Luma', 'description': 'Gray elephant'}],
             'shots': [{'prompt': 'Luma walks through the forest.', 'duration': 10, 'character_ids': ['draft-role']}]}
    workflow.save_draft(pid, draft, 0)
    with core.connect() as db:
        db.execute('DELETE FROM guided_characters WHERE project_id=?', (pid,))
    state = characters.import_existing_bible(pid, workflow.get(pid)['revision'])
    assert state['characters'][0]['id'] == 'draft-role'
    assert workflow.get(pid)['draft']['shots'][0]['character_ids'] == ['draft-role']


def test_new_project_requires_explicit_empty_cast(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Landscape only', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'The river flows through a forest.', 'duration': 10, 'character_ids': []}]}, 0)
    with pytest.raises(ValueError, match='characters'):
        workflow.confirm_storyboard(pid, state['revision'])
    state = characters.confirm_empty_cast(pid, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    assert state['storyboard_confirmed']


def test_saving_suggested_characters_preserves_draft_and_creates_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest story', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    draft = {'character_bible': {'Luma': 'elephant'},
             'characters': [{'id': 'role-1', 'name': 'Luma', 'description': 'Small gray elephant'}],
             'shots': [{'prompt': 'Luma walks through the forest.', 'duration': 10, 'character_ids': ['role-1']}]}
    state = workflow.save_draft(pid, draft, 0)
    assert state['draft'] == draft
    assert characters.get(pid)['characters'][0]['id'] == 'role-1'
    with pytest.raises(ValueError, match='characters'):
        workflow.confirm_storyboard(pid, state['revision'])


def test_changing_role_invalidates_only_linked_shots_and_requires_keep_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest story', 20, 'mock', 5)
    workflow.create(pid)
    d = {'character_bible': {}, 'shots': [{'prompt': f'Forest shot number {i}.', 'duration': 5} for i in range(1, 5)]}
    state = workflow.save_draft(pid, d, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    state = characters.activate(pid, workflow.get(pid)['revision'])
    state = characters.save_character(pid, None, 'A', 'Elephant', state['revision'])
    state = characters.save_character(pid, None, 'B', 'Parrot', state['revision'])
    a, b = [r['id'] for r in state['characters']]
    files = {}
    for cid, image_id in [(a, 'a1'), (a, 'a2'), (b, 'b1')]:
        f = tmp_path / image_id
        f.write_bytes(image_id.encode())
        with core.connect() as db:
            db.execute('INSERT INTO guided_character_images VALUES (?,?,?,?,?,?,?)',
                       (image_id, pid, cid, str(f), timeline._hash(f), 'test', core.now()))
    state = characters.confirm_character(pid, a, 'a1', state['revision'])
    state = characters.confirm_character(pid, b, 'b1', state['revision'])
    for seq, ids in [(1, [a]), (2, [b]), (3, [a, b]), (4, [])]:
        state = characters.set_shot_characters(pid, seq, ids, characters.get(pid)['revision'])
        f = tmp_path / f'video{seq}.mp4'
        f.write_bytes(f'video{seq}'.encode())
        files[seq] = f
        asset = timeline.import_asset(pid, seq, str(f))
        timeline.select(pid, seq, asset)
        shot = core.storyboard(pid)['shots'][seq - 1]
        with core.connect() as db:
            db.execute('INSERT INTO guided_confirmations VALUES (?,?,?,?)', (pid, shot['id'], asset, shot['prompt']))
            db.execute('INSERT INTO guided_frame_versions VALUES (?,?,?,?,?,0)', (f'frame{seq}', pid, shot['id'], str(f), timeline._hash(f)))
            for cid in ids:
                db.execute('INSERT INTO guided_frame_characters VALUES (?,?,?)', (f'frame{seq}', cid, 'a1' if cid == a else 'b1'))
    with core.connect() as db:
        db.execute('DELETE FROM guided_stale_shots WHERE project_id=?', (pid,))
        db.execute('UPDATE guided_character_settings SET storyboard_reconfirmed=1 WHERE project_id=?', (pid,))
    state = characters.select_character_image(pid, a, 'a2', workflow.get(pid)['revision'])
    assert not next(r for r in state['characters'] if r['id'] == a)['confirmed']
    with core.connect() as db:
        stale = [r['id'] for r in db.execute('SELECT id FROM guided_frame_versions WHERE stale=1 ORDER BY id')]
    assert stale == ['frame1', 'frame3']
    assert not workflow.render_ready(pid)
    state = characters.confirm_character(pid, a, 'a2', state['revision'])
    for seq in (1, 3):
        asset = timeline.timeline(pid)[seq - 1]['selected']['id']
        state = characters.keep_old_video(pid, seq, asset, state['revision'])
    assert workflow.render_ready(pid)
    assert all(f.read_bytes() == f'video{seq}'.encode() for seq, f in files.items())
