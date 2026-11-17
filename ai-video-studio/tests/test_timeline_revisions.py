"""Task 3: copy-on-write timeline revisions, validation, and legacy projection."""
import pytest
from fastapi.testclient import TestClient

from studio import core, edit_model, timeline, timeline_revisions, webapp


@pytest.fixture
def timeline_revision(tmp_path, monkeypatch):
    """A non-guided project with three 5-second shots, each with a fake asset.

    Legacy materialization tiles them contiguously at positions 1..3:
        shot1 [0,5s], shot2 [5s,10s], shot3 [10s,15s].
    """
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Timeline revisions', 15, 'mock', 0)
    core.plan(pid, 5)
    root = core.HOME / pid
    with core.connect() as db:
        shots = db.execute(
            'SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchall()
    asset_ids = {}
    for shot in shots:
        path = root / f'{shot["id"]}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'timeline-' + shot['id'].encode())
        asset_ids[shot['id']] = timeline.import_asset(pid, shot['seq'], str(path))
    timeline.sync(pid)
    rid = edit_model.ensure_legacy_revisions(pid)['timeline_revision_id']
    return pid, rid, shots, asset_ids


def test_current_returns_video_clips(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    current = timeline_revisions.current(pid)
    assert current['id'] == rid
    assert [c['shot_id'] for c in current['video_clips']] == [s['id'] for s in shots]
    assert [c['position'] for c in current['video_clips']] == [1, 2, 3]


@pytest.mark.parametrize('shot_key,source_in,source_out,position,error', [
    (1, 0, 10_000_000, 2, 'source exceeds asset'),
    (1, 0, 2_000_000, 4, 'gap'),
    (1, 0, 2_000_000, 1, 'overlap'),
])
def test_timeline_validation_rejects_gap_overlap_and_source_overflow(
        timeline_revision, shot_key, source_in, source_out, position, error):
    pid, rid, shots, asset_ids = timeline_revision
    shot = shots[shot_key]
    with pytest.raises(ValueError, match=error):
        timeline_revisions.set_video_clip(
            pid, rid, shot['id'], asset_ids[shot['id']],
            source_in_us=source_in, source_out_us=source_out, position=position)


def test_set_video_clip_rejects_missing_asset(timeline_revision):
    pid, rid, shots, _ = timeline_revision
    with pytest.raises(ValueError, match='missing asset'):
        timeline_revisions.set_video_clip(
            pid, rid, shots[0]['id'], 'no-such-asset',
            source_in_us=0, source_out_us=2_000_000, position=1)


def test_set_video_clip_rejects_invalid_duration(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    with pytest.raises(ValueError, match='invalid duration'):
        timeline_revisions.set_video_clip(
            pid, rid, shots[0]['id'], asset_ids[shots[0]['id']],
            source_in_us=4_000_000, source_out_us=2_000_000, position=1)


def test_set_video_clip_is_copy_on_write(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    shot = shots[2]  # trim the last clip: no gap/overlap results
    new = timeline_revisions.set_video_clip(
        pid, rid, shot['id'], asset_ids[shot['id']],
        source_in_us=0, source_out_us=2_000_000, position=3)
    assert new['id'] != rid
    # Old revision is unchanged.
    old = timeline_revisions.get(pid, rid)
    assert old['video_clips'][2]['source_out_us'] == 5_000_000
    # New revision reflects the trim; earlier clips are untouched.
    assert new['video_clips'][2]['source_out_us'] == 2_000_000
    assert [c['shot_id'] for c in new['video_clips']] == [s['id'] for s in shots]


def test_validate_returns_errors_for_hand_built_bad_revision(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    # Hand-craft an invalid revision: overlap + gap + missing asset + bad duration.
    from uuid import uuid4
    with core.connect() as db:
        bad_id = uuid4().hex
        db.execute(
            'INSERT INTO timeline_revisions(id,project_id,storyboard_revision_id,revision_no,fps,duration_us,source,actor,created) '
            'VALUES (?,?,?,99,24,?,?,?,?)',
            (bad_id, pid, timeline_revisions.get(pid, rid)['storyboard_revision_id'],
             15 * 1_000_000, 'manual', 'human', core.now()))
        db.execute(
            'INSERT INTO timeline_video_clips(id,timeline_revision_id,shot_id,asset_id,position,'
            'source_in_us,source_out_us,timeline_in_us,timeline_out_us,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (uuid4().hex, bad_id, shots[0]['id'], asset_ids[shots[0]['id']], 1,
             0, 5_000_000, 1_000_000, 6_000_000, core.now()))  # gap at start
    errors = timeline_revisions.validate(pid, bad_id)
    assert any(e['type'] == 'gap' for e in errors)
    # A valid revision validates clean.
    assert timeline_revisions.validate(pid, rid) == []


def test_create_draft_clones_current(timeline_revision):
    pid, rid, shots, _ = timeline_revision
    draft = timeline_revisions.create_draft(pid, actor='human')
    assert draft['id'] != rid
    assert [c['shot_id'] for c in draft['video_clips']] == [s['id'] for s in shots]
    assert timeline_revisions.current(pid)['id'] == draft['id']


def test_add_audio_clip_preserves_video_clips(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    audio = core.HOME / pid / 'voice.wav'
    audio.write_bytes(b'fake-audio')
    before = timeline_revisions.current(pid)
    new = timeline_revisions.add_audio_clip(
        pid, rid, kind='sfx', file=str(audio), source_out_us=2_000_000,
        timeline_in_us=1_000_000, gain=0.5, actor='human')
    assert new['id'] != rid
    assert [c['shot_id'] for c in new['video_clips']] == \
        [c['shot_id'] for c in before['video_clips']]
    assert len(new['audio_clips']) == len(before['audio_clips']) + 1
    assert new['audio_clips'][-1]['kind'] == 'sfx'
    assert new['audio_clips'][-1]['gain'] == 0.5


def test_set_subtitles_preserves_video_and_audio(timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    srt = core.HOME / pid / 'captions.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:02,000\nhello\n')
    before = timeline_revisions.current(pid)
    new = timeline_revisions.set_subtitles(pid, rid, srt_file=str(srt))
    assert new['id'] != rid
    assert [c['shot_id'] for c in new['video_clips']] == \
        [c['shot_id'] for c in before['video_clips']]
    assert new['subtitles'] and new['subtitles'][0]['srt_file'] == str(srt.resolve())


def test_sound_edits_record_revision_without_touching_video(timeline_revision):
    from studio import sound
    pid, rid, shots, asset_ids = timeline_revision
    before = timeline_revisions.current(pid)
    video_before = [c['shot_id'] for c in before['video_clips']]
    srt = core.HOME / pid / 'captions.srt'
    srt.write_text('1\n00:00:00,000 --> 00:00:02,000\nhello\n')
    sound.configure(pid, subtitles=str(srt))
    after = timeline_revisions.current(pid)
    assert after['id'] != before['id']
    assert [c['shot_id'] for c in after['video_clips']] == video_before
    assert after['subtitles'] and after['subtitles'][0]['srt_file'] == str(srt.resolve())


@pytest.fixture
def timeline_project(tmp_path, monkeypatch):
    """A one-shot project with two assets; the current clip uses the newest asset."""
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    pid = core.create('Legacy selection', 10, 'mock', 0)
    core.plan(pid, 10)
    with core.connect() as db:
        shot = db.execute(
            'SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchone()
    root = core.HOME / pid
    first = root / 'first.mp4'
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b'first-asset')
    first_id = timeline.import_asset(pid, shot['seq'], str(first))
    second = root / 'second.mp4'
    second.write_bytes(b'second-asset')
    second_id = timeline.import_asset(pid, shot['seq'], str(second))
    timeline.sync(pid)  # selects the most recently created asset (second)
    edit_model.ensure_legacy_revisions(pid)
    return pid, shot['id'], first_id


def test_legacy_selection_writes_revision_and_projection(timeline_project):
    pid, shot_id, asset_id = timeline_project
    before = edit_model.current(pid)['timeline_revision_id']
    timeline.select(pid, 1, asset_id)
    after = edit_model.current(pid)['timeline_revision_id']
    assert after != before
    with core.connect() as db:
        assert db.execute(
            'SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?',
            (pid, shot_id)).fetchone()[0] == asset_id
    assert timeline_revisions.get(pid, after)['video_clips'][0]['asset_id'] == asset_id


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path / 'data')
    return TestClient(webapp.app)


def test_timeline_revisions_api(api, timeline_revision):
    pid, rid, shots, asset_ids = timeline_revision
    # List revisions.
    listed = api.get(f'/api/projects/{pid}/timeline/revisions')
    assert listed.status_code == 200
    assert any(r['id'] == rid for r in listed.json())
    # Get a specific revision.
    detail = api.get(f'/api/projects/{pid}/timeline/revisions/{rid}')
    assert detail.status_code == 200
    assert detail.json()['id'] == rid
    assert len(detail.json()['video_clips']) == 3
    # Create a draft.
    created = api.post(f'/api/projects/{pid}/timeline/revisions', json={})
    assert created.status_code == 200
    assert created.json()['id'] != rid
    # Patch a clip (copy-on-write): trim the last clip so no gap/overlap results.
    shot = shots[2]
    patched = api.patch(
        f'/api/projects/{pid}/timeline/revisions/{rid}/clips/{shot["id"]}',
        json={'asset_id': asset_ids[shot['id']], 'source_in_us': 0,
              'source_out_us': 2_000_000, 'position': 3, 'actor': 'human'})
    assert patched.status_code == 200
    assert patched.json()['video_clips'][2]['source_out_us'] == 2_000_000
    # Invalid patch surfaces as 400.
    bad = api.patch(
        f'/api/projects/{pid}/timeline/revisions/{rid}/clips/{shot["id"]}',
        json={'asset_id': asset_ids[shot['id']], 'source_in_us': 0,
              'source_out_us': 10_000_000, 'position': 3})
    assert bad.status_code == 400
