"""Task 4: dependency invalidation and readiness policy."""
import pytest

from studio import core, edit_model, edit_readiness, storyboard_revisions, timeline, timeline_revisions


@pytest.fixture
def readiness_project(tmp_path, monkeypatch):
    """Two 5-second shots, each with a selected fake asset and a materialized
    timeline. No decisions exist yet, so the project is a clean automatic
    (non-guided) baseline."""
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Readiness project', 10, 'mock', 0)
    core.plan(pid, 5)
    root = core.HOME / pid
    with core.connect() as db:
        shots = db.execute(
            'SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchall()
    asset_ids = {}
    for shot in shots:
        path = root / f'{shot["id"]}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'asset-' + shot['id'].encode())
        asset_ids[shot['id']] = timeline.import_asset(pid, shot['seq'], str(path))
    timeline.sync(pid)
    edit_model.ensure_legacy_revisions(pid)
    return pid, [s['id'] for s in shots], asset_ids


def _shot_ids(status):
    return [s['shot_id'] for s in status['shots']]


def test_audio_and_subtitle_edits_only_invalidate_mix_and_export(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    before = edit_readiness.status(pid, mode='guided')
    rid = timeline_revisions.current(pid)['id']
    audio = core.HOME / pid / 'voice.wav'
    audio.write_bytes(b'fake-audio')
    rid = timeline_revisions.add_audio_clip(
        pid, rid, kind='sfx', file=str(audio), source_out_us=2_000_000)['id']
    srt = core.HOME / pid / 'captions.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:02,000\nhello\n')
    timeline_revisions.set_subtitles(pid, rid, srt_file=str(srt))
    after = edit_readiness.status(pid, mode='guided')
    assert after['shots'] == before['shots']
    assert after['mix_stale'] and after['export_stale']
    assert not after['video_stale']


def test_automatic_mode_uses_system_decisions_without_fake_human_confirmation(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    before = edit_readiness.status(pid, mode='automatic')
    assert before['shots'][0]['ready'] is False
    storyboard_revisions.record_decision(
        pid, node_type='shot', node_id=shot_ids[0], action='accept', actor='system')
    status = edit_readiness.status(pid, mode='automatic')
    assert status['shots'][0]['ready'] is True
    assert all(d['actor'] != 'human' for d in status['decisions'])


def test_prompt_edit_invalidates_shot_asset_and_export_only_that_shot(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    rid = storyboard_revisions.current(pid)['id']
    storyboard_revisions.update_shot(pid, rid, shot_ids[0], prompt='Changed', actor='human')
    status = edit_readiness.status(pid, mode='automatic')
    by_id = {s['shot_id']: s for s in status['shots']}
    assert by_id[shot_ids[0]]['status'] == 'stale'
    assert by_id[shot_ids[0]]['reasons']
    assert by_id[shot_ids[1]]['status'] == 'valid'
    assert status['video_stale'] is True
    assert status['export_stale'] is True
    assert status['mix_stale'] is False


def test_clip_source_range_invalidates_export_only(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    before = edit_readiness.status(pid, mode='automatic')
    rid = timeline_revisions.current(pid)['id']
    timeline_revisions.set_video_clip(
        pid, rid, shot_ids[1], asset_ids[shot_ids[1]],
        source_in_us=0, source_out_us=2_000_000, position=2)
    after = edit_readiness.status(pid, mode='automatic')
    assert after['export_stale'] is True
    assert after['video_stale'] is False
    assert after['mix_stale'] is False
    assert after['shots'] == before['shots']


def test_reorder_invalidates_export_only(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    rid = storyboard_revisions.current(pid)['id']
    storyboard_revisions.reorder(pid, rid, list(reversed(shot_ids)), actor='human')
    status = edit_readiness.status(pid, mode='automatic')
    assert status['export_stale'] is True
    assert status['video_stale'] is False
    assert status['mix_stale'] is False


def test_invalidate_returns_affected_shot_ids_for_character(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    cid = 'char-0001'
    with core.connect() as db:
        db.execute('INSERT INTO guided_characters(id,project_id,name,description) VALUES (?,?,?,?)',
                   (cid, pid, 'Alice', 'A woman in red'))
        db.execute('INSERT INTO guided_shot_characters(project_id,shot_id,character_id) VALUES (?,?,?)',
                   (pid, shot_ids[0], cid))
    affected = edit_readiness.invalidate(pid, node_type='character', node_id=cid,
                                         reason='character changed')
    assert affected == [shot_ids[0]]
    status = edit_readiness.status(pid, mode='automatic')
    assert {s['shot_id']: s['status'] for s in status['shots']}[shot_ids[0]] == 'stale'


def test_invalidate_returns_adjacent_shot_ids_for_boundary(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    with core.connect() as db:
        db.execute('INSERT INTO guided_boundaries(id,project_id,position,confirmed_image_id,active,created) '
                   'VALUES (?,?,?,?,?,?)', ('b-0001', pid, 1, None, 1, core.now()))
    affected = edit_readiness.invalidate(pid, node_type='boundary', node_id='b-0001',
                                         reason='boundary changed')
    assert affected == shot_ids  # boundary position 1 touches both shots


def test_keep_old_resolves_staleness_in_guided_mode(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    rid = storyboard_revisions.current(pid)['id']
    storyboard_revisions.update_shot(pid, rid, shot_ids[0], prompt='Changed', actor='human')
    assert edit_readiness.status(pid, mode='guided')['shots'][0]['status'] == 'stale'
    storyboard_revisions.record_decision(
        pid, node_type='shot', node_id=shot_ids[0], action='keep_old', actor='human',
        metadata={'asset_id': asset_ids[shot_ids[0]]})
    status = edit_readiness.status(pid, mode='guided')
    assert status['shots'][0]['status'] == 'valid'
    assert status['shots'][0]['ready'] is True


def test_require_ready_raises_listing_blockers_and_passes(readiness_project):
    pid, shot_ids, asset_ids = readiness_project
    rid = storyboard_revisions.current(pid)['id']
    storyboard_revisions.update_shot(pid, rid, shot_ids[0], prompt='Changed', actor='human')
    with pytest.raises(ValueError, match='stale'):
        edit_readiness.require_ready(pid, mode='automatic')
    # A system select resolves shot 0's staleness; both shots need a decision.
    storyboard_revisions.record_decision(
        pid, node_type='shot', node_id=shot_ids[0], action='select', actor='system',
        metadata={'asset_id': asset_ids[shot_ids[0]]})
    storyboard_revisions.record_decision(
        pid, node_type='shot', node_id=shot_ids[1], action='accept', actor='system')
    edit_readiness.require_ready(pid, mode='automatic')


def test_legacy_stale_shot_marks_shot_stale(readiness_project):
    """An unresolved guided_stale_shots keep decision must read as video-stale."""
    pid, shot_ids, asset_ids = readiness_project
    with core.connect() as db:
        db.execute('INSERT INTO guided_stale_shots(project_id,shot_id,keep_asset_id) '
                   'VALUES (?,?,NULL)', (pid, shot_ids[0]))
    status = edit_readiness.status(pid, mode='guided')
    by_id = {s['shot_id']: s for s in status['shots']}
    assert by_id[shot_ids[0]]['status'] == 'stale'
    assert any('keep_old' in r for r in by_id[shot_ids[0]]['reasons'])
    assert by_id[shot_ids[1]]['status'] == 'valid'
    assert status['video_stale'] is True


def test_resolved_legacy_stale_shot_is_not_stale(readiness_project):
    """A keep decision that matches the selected asset resolves legacy staleness."""
    pid, shot_ids, asset_ids = readiness_project
    with core.connect() as db:
        db.execute('INSERT INTO guided_stale_shots(project_id,shot_id,keep_asset_id) '
                   'VALUES (?,?,?)', (pid, shot_ids[0], asset_ids[shot_ids[0]]))
    status = edit_readiness.status(pid, mode='guided')
    by_id = {s['shot_id']: s for s in status['shots']}
    assert by_id[shot_ids[0]]['status'] == 'valid'


def test_stale_boundary_frame_marks_adjacent_shots_stale(readiness_project):
    """A confirmed boundary frame marked stale invalidates its adjacent shots."""
    pid, shot_ids, asset_ids = readiness_project
    with core.connect() as db:
        db.execute('INSERT INTO guided_boundaries(id,project_id,position,'
                   'confirmed_image_id,active,created) VALUES (?,?,?,?,?,?)',
                   ('b-0001', pid, 1, 'v-0001', 1, core.now()))
        db.execute('INSERT INTO guided_boundary_versions(id,project_id,boundary_id,'
                   'file,sha256,stale,created) VALUES (?,?,?,?,?,?,?)',
                   ('v-0001', pid, 'b-0001', 'x.png', 's', 1, core.now()))
    status = edit_readiness.status(pid, mode='guided')
    by_id = {s['shot_id']: s for s in status['shots']}
    assert by_id[shot_ids[0]]['status'] == 'stale'
    assert by_id[shot_ids[1]]['status'] == 'stale'
    assert status['video_stale'] is True


def test_guided_structure_change_invalidates_export(tmp_path, monkeypatch):
    """guided_structure.apply mutates structure; readiness must flag export."""
    from studio import guided_structure, guided_workflow, guided_characters
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    guided_workflow.create(pid, character_stage_enabled=True)
    state = guided_characters.confirm_empty_cast(pid, 0)
    state = guided_workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant drinks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant sleeps.', 'duration': 8, 'character_ids': []}]},
        state['revision'])
    state = guided_workflow.confirm_storyboard(pid, state['revision'])
    shots = [{'id': s['id'], 'prompt': s['prompt'], 'duration': s['duration'],
              'character_ids': []} for s in state['shots']]
    guided_structure.apply(pid, shots, guided_workflow.get(pid)['revision'])
    status = edit_readiness.status(pid, mode='guided')
    assert status['export_stale'] is True
