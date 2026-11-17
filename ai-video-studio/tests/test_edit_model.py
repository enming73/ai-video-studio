"""Task 1: schema, project state, and legacy materialization."""
import pytest

from studio import core, edit_model, timeline


def shot_revisions(pid):
    with core.connect() as db:
        return [dict(r) for r in db.execute(
            'SELECT sr.* FROM shot_revisions sr '
            'JOIN storyboard_revisions sb ON sb.id = sr.storyboard_revision_id '
            'WHERE sb.project_id=? ORDER BY sr.seq', (pid,))]


def current_video_clips(pid):
    return timeline.current_video_clips(pid)


@pytest.fixture
def legacy_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('legacy materialization', 20, 'mock', 0)
    core.plan(pid, 10)
    root = core.HOME / pid
    with core.connect() as db:
        shots = db.execute(
            'SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchall()
    shot_ids = [s['id'] for s in shots]
    asset_ids = []
    for shot in shots:
        path = root / f'{shot["id"]}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'legacy-video-' + shot['id'].encode())
        asset_ids.append(timeline.import_asset(pid, shot['seq'], str(path)))
    timeline.sync(pid)
    with core.connect() as db:
        db.execute(
            'INSERT INTO render_history(id,project_id,output,manifest_json,created) '
            'VALUES (?,?,?,?,?)',
            ('rh-legacy', pid, str(root / 'final.mp4'), '{}', core.now()))
    return pid, shot_ids, asset_ids


def test_legacy_materialization_preserves_shot_ids_and_assets(legacy_project):
    pid, shot_ids, asset_ids = legacy_project
    revisions = edit_model.ensure_legacy_revisions(pid)
    assert revisions['storyboard_revision_id']
    assert revisions['timeline_revision_id']
    assert set(shot_ids) <= {r['shot_id'] for r in shot_revisions(pid)}
    assert set(asset_ids) <= {c['asset_id'] for c in current_video_clips(pid)}
    with core.connect() as db:
        assert db.execute(
            'SELECT COUNT(*) FROM shots WHERE project_id=?', (pid,)).fetchone()[0] == len(shot_ids)
        assert db.execute(
            'SELECT COUNT(*) FROM render_history WHERE project_id=?', (pid,)).fetchone()[0] >= 1


def test_legacy_materialization_is_idempotent(legacy_project):
    pid, shot_ids, asset_ids = legacy_project
    first = edit_model.ensure_legacy_revisions(pid)
    second = edit_model.ensure_legacy_revisions(pid)
    assert first == second
    with core.connect() as db:
        assert db.execute(
            'SELECT COUNT(*) FROM storyboard_revisions WHERE project_id=?', (pid,)).fetchone()[0] == 1
        assert db.execute(
            'SELECT COUNT(*) FROM timeline_revisions WHERE project_id=?', (pid,)).fetchone()[0] == 1


def test_current_lazily_materializes_and_returns_revisions(legacy_project):
    pid, shot_ids, asset_ids = legacy_project
    current = edit_model.current(pid)
    assert current['storyboard_revision_id']
    assert current['timeline_revision_id']
    assert current['storyboard_revision']['revision_no'] == 1
    assert current['storyboard_revision']['source'] == 'legacy'
    assert current['timeline_revision']['revision_no'] == 1
    assert current['timeline_revision']['source'] == 'legacy'


def test_sync_legacy_projection_matches_current_clips(legacy_project):
    pid, shot_ids, asset_ids = legacy_project
    edit_model.ensure_legacy_revisions(pid)
    edit_model.sync_legacy_projection(pid)
    with core.connect() as db:
        items = db.execute(
            'SELECT shot_id, asset_id FROM timeline_items '
            'WHERE project_id=? ORDER BY position', (pid,)).fetchall()
    assert [i['shot_id'] for i in items] == shot_ids
    assert set(i['asset_id'] for i in items) == set(asset_ids)
