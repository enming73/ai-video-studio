import pytest

from studio import core, guided_boundaries as boundaries, guided_characters as roles
from studio import guided_structure, guided_workflow as workflow, timeline
from tests.helpers import add_version, attach_video, make_png


@pytest.fixture
def structure_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant drinks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant sleeps.', 'duration': 8, 'character_ids': []}]}, state['revision'])
    return pid, workflow.confirm_storyboard(pid, state['revision'])


def target(state, durations):
    return [{'id': s['id'], 'prompt': s['prompt'], 'duration': d, 'character_ids': []}
            for s, d in zip(state['shots'], durations)]


def confirm(pid, position, state, name):
    version = add_version(pid, state['boundaries'][position]['id'], name, size='1536x864')
    return boundaries.confirm(pid, state['boundaries'][position]['id'], version,
                              workflow.get(pid)['revision'])


def test_total_duration_must_match(structure_project):
    pid, state = structure_project
    with pytest.raises(ValueError, match='sum'):
        guided_structure.apply(pid, target(state, [7, 6, 8]), state['revision'])


def test_confirmed_shot_duration_cannot_change_directly(structure_project):
    pid, state = structure_project
    attach_video(pid, state['shots'][0], 'a', 'b')
    with pytest.raises(ValueError, match='archive'):
        guided_structure.apply(pid, target(state, [8, 6, 6]), workflow.get(pid)['revision'])


def test_archive_preserves_asset_and_compacts_sequence(structure_project):
    pid, state = structure_project
    path = attach_video(pid, state['shots'][0], 'a', 'b')
    guided_structure.apply(pid, target(state, [6, 10, 10])[1:], workflow.get(pid)['revision'])
    assert path.exists()
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM shot_assets WHERE file=?', (str(path),)).fetchone()[0] == 1
        assert [r['seq'] for r in db.execute('SELECT seq FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,))] == [1, 2]
        assert db.execute('SELECT seq FROM shots WHERE project_id=? AND archived=1', (pid,)).fetchone()['seq'] < 0


def test_deleting_middle_shot_merges_boundaries_without_confirming(structure_project):
    pid, state = structure_project
    b = state['boundaries']
    confirm(pid, 1, state, 'end-s1')
    state = workflow.get(pid)
    confirm(pid, 2, state, 'start-s3')
    state = workflow.get(pid)
    attach_video(pid, state['shots'][0], b[0]['id'], b[1]['id'])
    attach_video(pid, state['shots'][2], b[2]['id'], b[3]['id'])
    current = workflow.get(pid)
    shots = target(current, [6, 6, 14])
    state = guided_structure.apply(pid, [shots[0], shots[2]], current['revision'])
    middle = state['boundaries'][1]
    assert middle['id'] not in {b[1]['id'], b[2]['id']}
    assert middle['confirmed'] is False
    assert middle['candidate_count'] == 2
    assert state['boundaries'][0]['id'] == b[0]['id']
    assert state['boundaries'][2]['id'] == b[3]['id']
    assert [s['old_video_decision_required'] for s in state['shots']] == [True, True]


def test_merge_deduplicates_shared_candidate_by_sha256(structure_project):
    pid, state = structure_project
    b = state['boundaries']
    with core.connect() as db:
        directory = core.HOME / pid / 'guided-images'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'shared.png'
        make_png(path, '1536x864')
        # The same file (hence the same sha256) is a candidate on both the end
        # boundary of shot 1 and the start boundary of shot 3. Merging them must
        # dedupe to a single candidate rather than copying the bytes twice.
        boundaries.insert_version(db, pid, b[1]['id'], path, stale=False, prompt='p', model='test', references=[])
        boundaries.insert_version(db, pid, b[2]['id'], path, stale=False, prompt='p', model='test', references=[])
    current = workflow.get(pid)
    shots = target(current, [6, 6, 14])
    state = guided_structure.apply(pid, [shots[0], shots[2]], current['revision'])
    assert state['boundaries'][1]['candidate_count'] == 1


def test_inserting_shot_creates_two_unconfirmed_boundaries(structure_project):
    pid, state = structure_project
    confirm(pid, 1, state, 'shared')
    current = workflow.get(pid)
    old = current['shots']
    shots = [
        {'id': old[0]['id'], 'prompt': old[0]['prompt'], 'duration': 6, 'character_ids': []},
        {'id': None, 'prompt': 'An elephant looks up.', 'duration': 4, 'character_ids': []},
        {'id': old[1]['id'], 'prompt': old[1]['prompt'], 'duration': 2, 'character_ids': []},
        {'id': old[2]['id'], 'prompt': old[2]['prompt'], 'duration': 8, 'character_ids': []}]
    state = guided_structure.apply(pid, shots, current['revision'])
    assert [b['confirmed'] for b in state['boundaries']] == [False, False, False, False, False]
    assert state['boundaries'][1]['candidate_count'] == 1
    assert state['boundaries'][2]['candidate_count'] == 1


def test_structure_is_rejected_while_a_paid_request_is_unresolved(structure_project):
    pid, state = structure_project
    with core.connect() as db:
        db.execute("INSERT INTO guided_image_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ('i1', pid, state['boundaries'][0]['id'], 'boundary', 'r1', 'm', 1, 'SUBMITTING', '{}', None, None))
    with pytest.raises(ValueError, match='unresolved'):
        guided_structure.apply(pid, target(state, [6, 6, 8]), state['revision'])
    with core.connect() as db:
        db.execute("UPDATE guided_image_attempts SET status='ACKNOWLEDGED'")
        db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ('v1', pid, state['shots'][0]['id'], 'r2', state['shots'][0]['prompt'], 6, 'minimax', 1, 'RUNNING'))
    with pytest.raises(ValueError, match='unresolved'):
        guided_structure.apply(pid, target(state, [6, 6, 8]), state['revision'])
