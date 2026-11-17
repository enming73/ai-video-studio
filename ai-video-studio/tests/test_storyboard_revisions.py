"""Task 2: copy-on-write storyboard revisions, shot versions, and decisions."""
import pytest
from fastapi.testclient import TestClient

from studio import core, edit_model, guided_workflow as workflow, storyboard_revisions, timeline_revisions, webapp


@pytest.fixture
def storyboard_revision(tmp_path, monkeypatch):
    """A non-guided project with one legacy shot whose prompt is 'Original'."""
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Legacy storyboard', 20, 'mock', 0)
    core.plan(pid, 10)
    with core.connect() as db:
        shot = db.execute(
            'SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchone()
        db.execute('UPDATE shots SET prompt=? WHERE id=?', ('Original', shot['id']))
    return pid, shot['id']


def test_storyboard_edit_creates_new_revision_and_preserves_old(storyboard_revision):
    pid, shot_id = storyboard_revision
    old = storyboard_revisions.current(pid)
    new = storyboard_revisions.update_shot(pid, old['id'], shot_id, prompt='Changed', actor='human')
    assert new['id'] != old['id']
    assert storyboard_revisions.get(pid, old['id'])['shots'][0]['prompt'] == 'Original'
    assert storyboard_revisions.get(pid, new['id'])['shots'][0]['prompt'] == 'Changed'
    assert shot_id in {s['shot_id'] for s in storyboard_revisions.get(pid, new['id'])['shots']}
    # The legacy projection reflects the current revision only.
    with core.connect() as db:
        assert db.execute('SELECT prompt FROM shots WHERE id=?', (shot_id,)).fetchone()[0] == 'Changed'


def test_update_shot_rejects_unknown_shot(storyboard_revision):
    pid, _ = storyboard_revision
    old = storyboard_revisions.current(pid)
    with pytest.raises(ValueError, match='Unknown shot'):
        storyboard_revisions.update_shot(pid, old['id'], 'no-such-shot', prompt='Changed')


def test_reorder_changes_sequence_and_preserves_old(storyboard_revision):
    pid, shot_id = storyboard_revision
    with core.connect() as db:
        ids = [r['id'] for r in db.execute(
            'SELECT id FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,))]
    assert len(ids) == 2
    old = storyboard_revisions.current(pid)
    reversed_ids = list(reversed(ids))
    new = storyboard_revisions.reorder(pid, old['id'], reversed_ids, actor='human')
    assert new['id'] != old['id']
    assert [s['shot_id'] for s in storyboard_revisions.get(pid, old['id'])['shots']] == ids
    assert [s['shot_id'] for s in storyboard_revisions.get(pid, new['id'])['shots']] == reversed_ids


def test_reorder_rejects_wrong_shot_set(storyboard_revision):
    pid, shot_id = storyboard_revision
    old = storyboard_revisions.current(pid)
    with pytest.raises(ValueError, match='match'):
        storyboard_revisions.reorder(pid, old['id'], [shot_id], actor='human')


def test_create_draft_clones_current_without_change(storyboard_revision):
    pid, _ = storyboard_revision
    current = storyboard_revisions.current(pid)
    draft = storyboard_revisions.create_draft(pid, actor='human', source='manual')
    assert draft['id'] != current['id']
    assert [s['prompt'] for s in draft['shots']] == [s['prompt'] for s in current['shots']]


def test_record_decision_persists_and_is_listed(storyboard_revision):
    pid, shot_id = storyboard_revision
    decision = storyboard_revisions.record_decision(
        pid, node_type='shot', node_id=shot_id, action='accept', actor='system',
        metadata={'prompt': 'auto'})
    assert decision['action'] == 'accept'
    assert decision['actor'] == 'system'
    assert decision['node_id'] == shot_id
    assert any(d['id'] == decision['id'] for d in storyboard_revisions.current(pid)['decisions'])


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return TestClient(webapp.app)


@pytest.fixture
def guided_storyboard(api):
    response = api.post('/api/production/projects', json={
        'prompt': 'Elephant in rainforest', 'duration': 20,
        'provider': 'mock', 'budget': 0, 'guided': True})
    assert response.status_code == 201
    pid = response.json()['project_id']
    with core.connect() as db:
        db.execute('DELETE FROM guided_character_settings WHERE project_id=?', (pid,))
    base = f'/api/projects/{pid}/guided'
    draft = {'character_bible': {}, 'shots': [
        {'prompt': 'The elephant wanders in a rainforest.', 'duration': 8},
        {'prompt': 'The elephant approaches a clear river.', 'duration': 12}]}
    r = api.put(base + '/draft', json={'revision': 0, 'draft': draft})
    r = api.post(base + '/storyboard/confirm', json={'revision': r.json()['revision']})
    assert r.status_code == 200
    return pid


def test_guided_shot_edit_writes_revision_and_decision(api, guided_storyboard):
    pid = guided_storyboard
    before = storyboard_revisions.current(pid)
    response = api.patch(f'/api/projects/{pid}/guided/shots/1',
                         json={'prompt': 'Changed', 'revision': workflow.get(pid)['revision']})
    assert response.status_code == 200
    after = storyboard_revisions.current(pid)
    assert after['id'] != before['id']
    assert any(d['action'] == 'accept' and d['actor'] == 'human' for d in after['decisions'])
    assert [s['prompt'] for s in storyboard_revisions.get(pid, before['id'])['shots']][0] \
        == 'The elephant wanders in a rainforest.'


def test_storyboard_revisions_api(api, storyboard_revision):
    pid, shot_id = storyboard_revision
    current = storyboard_revisions.current(pid)
    # List revisions.
    listed = api.get(f'/api/projects/{pid}/storyboard/revisions')
    assert listed.status_code == 200
    assert any(r['id'] == current['id'] for r in listed.json())
    # Get a specific revision.
    detail = api.get(f'/api/projects/{pid}/storyboard/revisions/{current["id"]}')
    assert detail.status_code == 200
    assert detail.json()['id'] == current['id']
    # Create a draft.
    created = api.post(f'/api/projects/{pid}/storyboard/revisions', json={})
    assert created.status_code == 200
    assert created.json()['id'] != current['id']
    # Patch a shot.
    patched = api.patch(f'/api/projects/{pid}/storyboard/revisions/{current["id"]}/shots/{shot_id}',
                        json={'prompt': 'Changed via API', 'actor': 'human'})
    assert patched.status_code == 200
    assert patched.json()['shots'][0]['prompt'] == 'Changed via API'
    # Reorder via structure.
    ids = [s['shot_id'] for s in storyboard_revisions.current(pid)['shots']]
    reordered = api.put(f'/api/projects/{pid}/storyboard/revisions/{patched.json()["id"]}/structure',
                        json={'ordered_shot_ids': list(reversed(ids))})
    assert reordered.status_code == 200
    assert [s['shot_id'] for s in reordered.json()['shots']] == list(reversed(ids))


def test_guided_confirmation_backfills_human_decision(api, guided_storyboard):
    pid = guided_storyboard
    # A legacy guided confirmation (pre-decision backfill) materializes as human.
    with core.connect() as db:
        shot = db.execute(
            'SELECT * FROM shots WHERE project_id=? AND seq=1', (pid,)).fetchone()
        db.execute('INSERT OR REPLACE INTO guided_confirmations VALUES (?,?,?,?)',
                   (pid, shot['id'], 'asset-legacy', shot['prompt']))
    decisions = storyboard_revisions.current(pid)['decisions']
    assert any(d['node_type'] == 'shot' and d['node_id'] == shot['id']
               and d['action'] == 'confirm' and d['actor'] == 'human' for d in decisions)


@pytest.fixture
def automatic_project(tmp_path, monkeypatch):
    """A non-guided project whose legacy revision is materialized."""
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Automatic revision truth', 20, 'mock', 0)
    core.plan(pid, 10)
    edit_model.ensure_legacy_revisions(pid)
    return pid


@pytest.fixture
def guided_project(tmp_path, monkeypatch):
    """A guided project with a confirmed storyboard and materialized revision."""
    from studio import guided_characters
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Guided revision truth', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = guided_characters.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks through the forest.', 'duration': 8, 'character_ids': []},
        {'prompt': 'An elephant reaches the river.', 'duration': 12, 'character_ids': []}]},
        state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    edit_model.ensure_legacy_revisions(pid)
    return pid


def test_guided_and_automatic_share_revision_truth(guided_project, automatic_project):
    """Both production modes resolve the same shared revision-truth interfaces.

    Regression: ``edit_model.current`` must return non-empty storyboard and
    timeline revision ids for guided and automatic projects alike, and both
    revision stores must be able to read the current revision back.
    """
    for pid in (guided_project, automatic_project):
        current = edit_model.current(pid)
        assert current['storyboard_revision_id']
        assert current['timeline_revision_id']
        assert storyboard_revisions.get(pid, current['storyboard_revision_id'])
        assert timeline_revisions.get(pid, current['timeline_revision_id'])
