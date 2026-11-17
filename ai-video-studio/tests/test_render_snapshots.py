"""Task 5: reproducible render snapshots and the local_render job."""
import json
import shutil
import subprocess

import pytest

from studio import core, edit_model, render_snapshots, sound, timeline, timeline_revisions


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


def test_local_render_resume_is_idempotent(snapshot_project):
    """A re-run after record_history (before SUCCEEDED) must not flip to FAILED."""
    from studio import attempts as attempts_store, jobs, worker
    from studio.handlers import local_render
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    done = render_snapshots.run_and_wait(pid, snap['id'])
    assert done['status'] == 'READY'

    job_id = render_snapshots._job_id(pid, snap['id'])
    job = jobs.get(job_id)
    control = worker.Control()
    # Simulate a resume that re-executes after a crash between record_history
    # and jobs.set_status(SUCCEEDED): it must not raise IntegrityError or mark
    # the already-rendered snapshot FAILED.
    result = local_render.execute(job, attempts_store, control)
    assert result['snapshot_id'] == snap['id']
    assert render_snapshots.get(pid, snap['id'])['status'] == 'READY'
    with core.connect() as db:
        count = db.execute('SELECT COUNT(*) FROM render_history WHERE id=?',
                           (snap['id'],)).fetchone()[0]
    assert count == 1


def test_record_history_is_idempotent(snapshot_project):
    pid, _rev, _srt = snapshot_project
    snap = render_snapshots.create(pid)
    manifest = snap['manifest']
    output = str(core.HOME / pid / 'renders' / snap['id'] / 'first.mp4')
    render_snapshots.record_history(pid, snap['id'], manifest, output)
    render_snapshots.record_history(pid, snap['id'], manifest, output)
    with core.connect() as db:
        count = db.execute('SELECT COUNT(*) FROM render_history WHERE id=?',
                           (snap['id'],)).fetchone()[0]
    assert count == 1


def test_trim_last_clip_recomputes_duration_and_renders(snapshot_project):
    """Trimming the last clip must recompute ``duration_us`` and still render.

    Regression: ``set_video_clip`` rewrote clip windows but left the timeline
    revision's ``duration_us`` at the old total, so trimming the final clip made
    ``local_render`` fail with ``Invalid clip duration``.
    """
    pid, rid, _other_srt = snapshot_project
    clips = timeline_revisions.current(pid)['video_clips']
    last = clips[-1]
    new = timeline_revisions.set_video_clip(
        pid, rid, last['shot_id'], last['asset_id'],
        source_in_us=0, source_out_us=1_000_000, position=last['position'])
    assert new['duration_us'] == 4_000_000  # 3s + 1s (was 6s)
    assert timeline_revisions.validate(pid, new['id']) == []
    out = timeline.render(pid, source='selected')
    assert out.is_file()
    core.verify_video(out, 4)


def test_subtitle_edit_via_revision_api_reaches_mix(snapshot_project):
    """A subtitle set via the revision API (not ``sound.configure``) must reach
    the burn. Regression: ``sound.export`` read ``soundtrack.json``, which the
    revision API never updates, so the subtitle was never burned."""
    pid, _rid, _other_srt = snapshot_project
    snap = render_snapshots.create(pid, actor='human')
    render_snapshots.run_and_wait(pid, snap['id'])
    out = sound.export(pid, burn_subtitles=True, snapshot_id=snap['id'])
    assert out.is_file()
    assert abs(sound._probe(out) - 6) < 0.5
    sidecar = json.loads(out.with_suffix('.json').read_text())
    assert sidecar['burn_subtitles'] is True
    assert sidecar['subtitle']['sha256'] == snap['manifest']['subtitle']['sha256']
    assert sidecar['subtitle']['srt_file'].endswith('captions.srt')


def _real_wav(root):
    wav = root / 'voice.wav'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', '-c:a', 'pcm_s16le', str(wav)],
                   check=True)
    return wav


def test_audio_edit_via_revision_api_reaches_mix(snapshot_project):
    """An audio clip added via the revision API must reach the mix."""
    pid, rid, _other_srt = snapshot_project
    wav = _real_wav(core.HOME / pid)
    rid = timeline_revisions.add_audio_clip(
        pid, rid, kind='sfx', file=str(wav), source_out_us=2_000_000,
        timeline_in_us=0, gain=0.5, actor='human')['id']
    snap = render_snapshots.create(pid, actor='human')
    render_snapshots.run_and_wait(pid, snap['id'])
    assert snap['manifest']['audio_clips'][-1]['sha256'] == edit_model._hash_file(str(wav))
    out = sound.export(pid, snapshot_id=snap['id'])
    assert out.is_file()
    sidecar = json.loads(out.with_suffix('.json').read_text())
    assert any(c['sha256'] == edit_model._hash_file(str(wav))
               for c in sidecar['audio_clips'])


def test_snapshot_mix_verifies_frozen_audio_hash(snapshot_project):
    """A file changed after the snapshot is refused at mix time, not silently used."""
    pid, rid, _other_srt = snapshot_project
    wav = _real_wav(core.HOME / pid)
    rid = timeline_revisions.add_audio_clip(
        pid, rid, kind='sfx', file=str(wav), source_out_us=2_000_000)['id']
    snap = render_snapshots.create(pid, actor='human')
    render_snapshots.run_and_wait(pid, snap['id'])
    wav.write_bytes(b'corrupted-after-snapshot')
    with pytest.raises(ValueError, match='changed since snapshot'):
        sound.export(pid, snapshot_id=snap['id'])


def test_later_audio_edit_does_not_change_existing_snapshot(snapshot_project):
    """An existing snapshot's frozen audio clips are unaffected by later edits."""
    pid, rid, _other_srt = snapshot_project
    first = render_snapshots.create(pid, actor='human')
    frozen = first['manifest']['audio_clips']
    audio = core.HOME / pid / 'voice.wav'
    audio.write_bytes(b'fake-audio')
    timeline_revisions.add_audio_clip(pid, rid, kind='sfx', file=str(audio),
                                      source_out_us=2_000_000)
    assert render_snapshots.get(pid, first['id'])['manifest']['audio_clips'] == frozen
