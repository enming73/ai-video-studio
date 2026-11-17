"""Task 5: reproducible render snapshots and the local_render job."""
import shutil

import pytest

from studio import core, edit_model, render_snapshots, timeline, timeline_revisions


@pytest.fixture
def snapshot_project(tmp_path, monkeypatch):
    """A two-shot project with real mock clips, selected assets, and one subtitle."""
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Snapshot project', 6, 'mock', 0)
    core.plan(pid, 3)
    core.generate(pid)
    timeline.sync(pid)
    edit_model.ensure_legacy_revisions(pid)
    root = core.HOME / pid
    srt = root / 'captions.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:03,000\nhello\n')
    other_srt = root / 'other.srt'
    other_srt.write_text('1\n00:00:00,000 --> 00:00:03,000\nworld\n')
    rid = timeline_revisions.current(pid)['id']
    rid = timeline_revisions.set_subtitles(pid, rid, srt_file=str(srt))['id']
    return pid, rid, other_srt


@pytest.fixture
def render_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Render project', 6, 'mock', 0)
    core.plan(pid, 3)
    core.generate(pid)
    timeline.sync(pid)
    edit_model.ensure_legacy_revisions(pid)
    return pid


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from studio import webapp
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    monkeypatch.setenv('STUDIO_DISABLE_EMBEDDED_WORKER', '1')
    return TestClient(webapp.app)


def test_snapshot_reexport_uses_frozen_inputs_after_edit(snapshot_project):
    pid, new_revision, other_srt = snapshot_project
    first = render_snapshots.create(pid, actor='human')
    first_output = render_snapshots.run_and_wait(pid, first['id'])['output']
    timeline_revisions.set_subtitles(pid, new_revision, srt_file=other_srt)
    second = render_snapshots.get(pid, first['id'])
    assert second['manifest']['subtitle']['sha256'] == first['manifest']['subtitle']['sha256']
    assert render_snapshots.reexport(pid, first['id'])['output'] != first_output


def test_create_freezes_a_pending_snapshot_with_manifest(snapshot_project):
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid, actor='human')
    assert snap['status'] == 'PENDING'
    assert snap['manifest']['project_id'] == pid
    assert len(snap['manifest']['video_clips']) == 2
    assert all(c['sha256'] for c in snap['manifest']['video_clips'])
    assert snap['manifest']['subtitle']['sha256']


def test_run_and_wait_produces_valid_video(snapshot_project):
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    done = render_snapshots.run_and_wait(pid, snap['id'])
    assert done['status'] == 'READY'
    assert done['output']
    core.verify_video(done['output'], 6)
    # The frozen manifest json sits next to the output.
    assert done['output'].endswith('.mp4')


def test_manifest_and_get_roundtrip(snapshot_project):
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    assert render_snapshots.manifest(pid, snap['id'])['video_clips'] == snap['manifest']['video_clips']
    with pytest.raises(ValueError):
        render_snapshots.get(pid, 'does-not-exist')


def test_reexport_never_overwrites(snapshot_project):
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    a = render_snapshots.reexport(pid, snap['id'])
    b = render_snapshots.reexport(pid, snap['id'])
    assert a['output'] != b['output']
    assert a['output'] != snap['output'] or snap['output'] is None
    import os
    assert os.path.exists(a['output'])
    assert os.path.exists(b['output'])


def test_create_rejects_stale_shots(snapshot_project):
    pid, _rev, _srt = snapshot_project
    from studio import storyboard_revisions
    sb = storyboard_revisions.current(pid)
    shot_id = sb['shots'][0]['shot_id']
    storyboard_revisions.update_shot(pid, sb['id'], shot_id, prompt='Changed', actor='human')
    with pytest.raises(ValueError, match='stale'):
        render_snapshots.create(pid)


def test_local_render_job_is_persistent(snapshot_project):
    from studio import jobs
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    listed = jobs.list_for_project(pid)
    assert any(j['kind'] == 'local_render' for j in listed)


def test_legacy_render_endpoint_returns_snapshot_id(api, render_project):
    pid = render_project
    response = api.post(f'/api/projects/{pid}/renders', json={'source': 'selected'})
    assert response.status_code == 202, response.text
    assert response.json()['id']
    assert render_snapshots.get(pid, response.json()['id'])['status'] in ('PENDING', 'READY')


def test_legacy_render_endpoint_completes_and_serves_video(api, render_project):
    import time
    pid = render_project
    response = api.post(f'/api/projects/{pid}/renders', json={'source': 'selected'})
    jid = response.json()['id']
    for _ in range(200):
        job = api.get(f'/api/projects/{pid}/renders/{jid}').json()
        if job['status'] in ('completed', 'failed'):
            break
        time.sleep(0.02)
    assert job['status'] == 'completed', job.get('error')
    assert api.get(f'/api/projects/{pid}/renders/{jid}/video').status_code == 200


def test_timeline_render_selected_returns_snapshot_output(snapshot_project):
    pid, _rev, _srt = snapshot_project
    out = timeline.render(pid, source='selected')
    assert out.is_file()
    core.verify_video(out, 6)
    with pytest.raises(ValueError):
        timeline.render(pid, output=out, source='selected')
