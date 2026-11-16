import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from studio import core, guided_characters as roles, guided_images as images, guided_workflow as workflow, webapp

MODEL = 'gpt-image-2.5-sunburst'


def png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant in a forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    return pid, state['characters'][0]['id'], state['revision']


def test_upload_versions_are_immutable_and_scoped(project):
    pid, cid, rev = project
    first = images.import_character_image(pid, cid, 'luma.png', png(), rev)
    second = images.import_character_image(pid, cid, 'luma2.png', png(), first['revision'])
    assert first['id'] != second['id']
    assert images.image_path(pid, first['id']).read_bytes() == png()
    assert len(images.list_character_images(pid, cid)) == 2
    assert 'file' not in first
    with pytest.raises(ValueError):
        images.image_path('not-a-project', first['id'])


@pytest.mark.parametrize('filename,content', [('../x.png', png()), ('x.png', b'\x89PNG\r\n\x1a\nforged'),
                                             ('x.exe', png()), ('x.png', b''), ('x.png', b'x' * (20 * 1024 * 1024 + 1))])
def test_unsafe_images_are_rejected_without_a_version(project, filename, content):
    pid, cid, rev = project
    with pytest.raises(ValueError):
        images.import_character_image(pid, cid, filename, content, rev)
    assert images.list_character_images(pid, cid) == []


def test_http_upload_has_no_server_path_and_rejects_stale_revision(project):
    pid, cid, rev = project
    api = TestClient(webapp.app)
    path = f'/api/projects/{pid}/guided/characters/{cid}/images'
    response = api.post(path, data={'revision': str(rev)}, files={'file': ('role.png', png(), 'image/png')})
    assert response.status_code == 200, response.text
    assert 'file' not in response.json()
    assert api.get(f'/api/projects/{pid}/guided/images/{response.json()["id"]}').content == png()
    assert api.post(path, data={'revision': str(rev)}, files={'file': ('role.png', png(), 'image/png')}).status_code == 400


def test_character_generation_requires_budget_and_is_idempotent(project):
    pid, cid, rev = project
    assert images.preview_character(pid, cid, MODEL)['estimated_cost_usd'] is None
    with pytest.raises(ValueError):
        images.generate_character(pid, cid, MODEL, 0, confirm_paid=True, request_id='zero', revision=rev)
    with pytest.raises(ValueError):
        images.generate_character(pid, cid, MODEL, 1, confirm_paid=False, request_id='no', revision=rev)
    result = images.generate_character(pid, cid, MODEL, 1, confirm_paid=True, request_id='same', revision=rev)
    again = images.generate_character(pid, cid, MODEL, 1, confirm_paid=True, request_id='same', revision=rev)
    assert result['status'] == 'QUEUED'
    assert again['id'] == result['id']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM guided_image_attempts').fetchone()[0] == 0


def test_character_image_double_click_reuses_job(project):
    pid, cid, rev = project
    first = images.generate_character(pid, cid, MODEL, 1, confirm_paid=True, request_id='a', revision=rev)
    second = images.generate_character(pid, cid, MODEL, 1, confirm_paid=True, request_id='b', revision=rev)
    assert first['id'] == second['id']
    assert first['status'] == 'QUEUED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM guided_image_attempts').fetchone()[0] == 0


def test_image_timeout_blocks_unknown_without_resubmit(tmp_path, monkeypatch):
    from studio import guided_compat, image_jobs, jobs, request_jobs, worker
    from studio.handlers import register_all
    register_all()
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant in a forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    character_id, revision = state['characters'][0]['id'], state['revision']
    images.generate_character(pid, character_id, MODEL, 1, confirm_paid=True, request_id='a', revision=revision)
    job = request_jobs.find(pid, kind='image', request_id='a')
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    monkeypatch.setattr(image_jobs, '_request_image',
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    assert worker.run_once() == job['id']
    assert jobs.get(job['id'])['block_reason'] == 'unknown_remote'
    assert guided_compat.find_image(pid, job['id'])['status'] == 'UNKNOWN'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (job['id'],)).fetchone()[0] == 'reserved'
    with pytest.raises(ValueError, match='unresolved'):
        image_jobs.enqueue_character(pid, character_id, MODEL, 1, confirm_paid=True,
                                     request_id='retry', revision=revision)


def test_invalid_provider_image_fails_with_reserved_cost(tmp_path, monkeypatch):
    from studio import image_jobs, jobs, request_jobs, worker
    from studio.handlers import register_all
    register_all()
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant in a forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    character_id, revision = state['characters'][0]['id'], state['revision']
    images.generate_character(pid, character_id, MODEL, 1, confirm_paid=True, request_id='a', revision=revision)
    job = request_jobs.find(pid, kind='image', request_id='a')
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    monkeypatch.setattr(image_jobs, '_request_image', lambda *a, **k: b'not a png')
    assert worker.run_once() == job['id']
    assert jobs.get(job['id'])['status'] == 'FAILED'
    with core.connect() as db:
        entry = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                           (job['id'],)).fetchone()
        assert entry['billing_status'] == 'reserved'
        attempt = db.execute('SELECT status, error_class FROM job_attempts WHERE job_id=?',
                             (job['id'],)).fetchone()
        assert attempt['status'] == 'FAILED_FINAL'
        assert attempt['error_class'] == 'content_rejected'
        assert db.execute('SELECT COUNT(*) FROM job_attempts WHERE job_id=?',
                          (job['id'],)).fetchone()[0] == 1


def test_image_missing_key_fails_and_releases(tmp_path, monkeypatch):
    from studio import jobs, request_jobs, worker
    from studio.handlers import register_all
    register_all()
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant in a forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    character_id, revision = state['characters'][0]['id'], state['revision']
    images.generate_character(pid, character_id, MODEL, 1, confirm_paid=True, request_id='a', revision=revision)
    job = request_jobs.find(pid, kind='image', request_id='a')
    monkeypatch.delenv('STUDIO_IMAGE_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    assert worker.run_once() == job['id']
    assert jobs.get(job['id'])['status'] == 'FAILED'
    with core.connect() as db:
        entry = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                           (job['id'],)).fetchone()
        assert entry['billing_status'] == 'released'


def test_concurrent_duplicate_image_request_makes_one_submission(project):
    pid, cid, rev = project
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(images.generate_character, pid, cid, MODEL, 1,
                            confirm_paid=True, request_id='same', revision=rev)
        second = pool.submit(images.generate_character, pid, cid, MODEL, 1,
                             confirm_paid=True, request_id='same', revision=rev)
        a, b = first.result(timeout=10), second.result(timeout=10)
    assert a['id'] == b['id']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM guided_image_attempts').fetchone()[0] == 0


def test_image_attempts_merge_legacy_and_jobs(project):
    from studio import guided_compat
    pid, cid, rev = project
    images.generate_character(pid, cid, MODEL, 1, confirm_paid=True, request_id='new', revision=rev)
    with core.connect() as db:
        db.execute('INSERT INTO guided_image_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   ('legacy1', pid, cid, 'character', 'old', MODEL, 1, 'UNKNOWN', '{}', None, 'legacy failure'))
    rows = guided_compat.image_attempt_records(pid)
    assert rows[0]['request_id'] == 'new'
    assert rows[0]['status'] == 'QUEUED'
    assert rows[1]['id'] == 'legacy1'
    assert set(rows[0]) == {'id', 'target_id', 'kind', 'request_id', 'model', 'budget',
                            'status', 'image_id', 'error'}
    assert guided_compat.find_image(pid, 'legacy1')['request_id'] == 'old'
    assert guided_compat.find_image(pid, rows[0]['id'])['request_id'] == 'new'


def test_image_projection_raises_unmappable_status(project):
    from studio import guided_compat, jobs, request_jobs
    pid, cid, rev = project
    job = request_jobs.submit(pid, kind='image', request_id='paused-img',
                              payload={'target': 'character', 'project_id': pid, 'target_id': cid,
                                       'model': MODEL, 'budget_usd': 1, 'request_id': 'paused-img',
                                       'prompt': 'Character reference portrait'},
                              budget_usd=1, target_key='character:paused-img')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='PAUSED' WHERE id=?", (job['id'],))
    with pytest.raises(ValueError, match='Unsupported image'):
        guided_compat.project_image_job(jobs.get(job['id']))
    # The list skips the unmappable entry instead of raising.
    assert guided_compat.image_attempt_records(pid) == []


def test_selecting_video_from_current_boundaries_resolves_old_video_decision(tmp_path, monkeypatch):
    from studio import timeline, guided_boundaries as boundaries
    from tests.helpers import add_version
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'The river flows in a quiet forest.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    shot_id = core.storyboard(pid)['shots'][0]['id']
    start_id = add_version(pid, state['boundaries'][0]['id'], 'start', size='1536x864')
    end_id = add_version(pid, state['boundaries'][1]['id'], 'end', size='1536x864')
    boundaries.confirm(pid, state['boundaries'][0]['id'], start_id, state['revision'])
    boundaries.confirm(pid, state['boundaries'][1]['id'], end_id, workflow.get(pid)['revision'])
    video = tmp_path / 'video.mp4'; video.write_bytes(b'video')
    asset = timeline.import_asset(pid, 1, str(video))
    with core.connect() as db:
        db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL)', (pid, shot_id))
        db.execute('INSERT INTO guided_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('attempt',pid,shot_id,'request','prompt',10,'mock',0,'COMPLETED',None,None,str(video),asset,None))
        db.execute('INSERT INTO guided_video_provenance(attempt_id,frame_id,frame_sha256,model,last_frame_id,last_frame_sha256) VALUES (?,?,?,?,?,?)',
                   ('attempt',start_id,'hash','mock',end_id,'hash2'))
    timeline.select(pid, 1, asset)
    assert not workflow.get(pid)['shots'][0]['old_video_decision_required']


def test_selecting_unknown_video_requires_explicit_keep_decision(tmp_path, monkeypatch):
    from studio import timeline
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A quiet forest beside the river.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    workflow.confirm_storyboard(pid, state['revision'])
    path = tmp_path/'external.mp4'; path.write_bytes(b'old video')
    aid = timeline.import_asset(pid, 1, str(path))
    timeline.select(pid, 1, aid)
    assert workflow.get(pid)['shots'][0]['old_video_decision_required']


def test_syncing_unknown_video_requires_explicit_keep_decision(tmp_path, monkeypatch):
    from studio import timeline
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A quiet forest beside the river.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    workflow.confirm_storyboard(pid, state['revision'])
    path = tmp_path/'external.mp4'; path.write_bytes(b'old video')
    with core.connect() as db:
        db.execute('UPDATE shots SET file=? WHERE project_id=?', (str(path), pid))
    timeline.sync(pid)
    assert workflow.get(pid)['shots'][0]['old_video_decision_required']
