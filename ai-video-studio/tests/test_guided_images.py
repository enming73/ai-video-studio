import struct
import zlib
import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi.testclient import TestClient
from studio import core, guided_characters as roles, guided_images as images, guided_workflow as workflow, webapp


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


def test_character_generation_requires_budget_and_is_idempotent(project, monkeypatch):
    pid, cid, rev = project
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    calls = []
    monkeypatch.setattr(images, '_image_request', lambda *a, **kw: calls.append(1) or {'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    assert images.preview_character(pid, cid, 'gpt-image-2.5-sunburst')['estimated_cost_usd'] is None
    with pytest.raises(ValueError):
        images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 0, confirm_paid=True, request_id='zero', revision=rev)
    with pytest.raises(ValueError):
        images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 1, confirm_paid=False, request_id='no', revision=rev)
    assert calls == []
    result = images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='same', revision=rev)
    again = images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='same', revision=rev)
    assert result['status'] == 'COMPLETED'
    assert again['id'] == result['id']
    assert len(calls) == 1
    assert images.image_path(pid, result['image_id']).read_bytes() == png()


def test_unknown_image_submission_blocks_automatic_retry(project, monkeypatch):
    pid, cid, rev = project
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    def failed(*args, **kwargs): raise OSError('unknown network outcome')
    monkeypatch.setattr(images, '_image_request', failed)
    result = images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='a', revision=rev)
    assert result['status'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='unresolved'):
        images.generate_character(pid, cid, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='b', revision=rev)


def test_concurrent_duplicate_image_request_makes_one_submission(project, monkeypatch):
    pid, cid, rev = project
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    entered, release, calls = Event(), Event(), []
    def image_request(*args, **kwargs):
        calls.append(1); entered.set()
        assert release.wait(5)
        return {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
    monkeypatch.setattr(images, '_image_request', image_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(images.generate_character, pid, cid, 'gpt-image-2.5-sunburst', 1,
                            confirm_paid=True, request_id='same', revision=rev)
        assert entered.wait(5)
        duplicate = pool.submit(images.generate_character, pid, cid, 'gpt-image-2.5-sunburst', 1,
                                confirm_paid=True, request_id='same', revision=rev).result(timeout=5)
        assert duplicate['status'] == 'SUBMITTING'
        release.set()
        assert first.result(timeout=10)['status'] == 'COMPLETED'
    assert len(calls) == 1


def test_first_frame_requires_approved_character_and_storyboard(project):
    pid, cid, rev = project
    draft = {'character_bible': {}, 'shots': [{'prompt': 'An elephant walks across the forest.', 'duration': 10, 'character_ids': [cid]}]}
    workflow.save_draft(pid, draft, rev)
    with pytest.raises(ValueError):
        images.preview_frame(pid, 1, 'gpt-image-2.5-sunburst')


def test_later_first_frame_is_blocked_until_previous_video_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A quiet landscape', 20, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt':'A quiet river flows through the forest.', 'duration':10,'character_ids':[]},
        {'prompt':'A sunny mountain stands in the distance.', 'duration':10,'character_ids':[]}]}, state['revision'])
    workflow.confirm_storyboard(pid, state['revision'])
    with pytest.raises(ValueError, match='earlier'):
        images.preview_frame(pid, 2, 'gpt-image-2.5-sunburst')


def test_no_character_first_frame_generation_is_budgeted_and_versioned(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    pid = core.create('A quiet empty landscape', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A quiet river flows through the forest.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    monkeypatch.setattr(images, '_image_request', lambda *a, **kw: {'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    plan = images.preview_frame(pid, 1, 'gpt-image-2.5-sunburst')
    assert plan['references'] == []
    with pytest.raises(ValueError):
        images.generate_frame(pid, 1, 'gpt-image-2.5-sunburst', 0, confirm_paid=True, request_id='bad', revision=state['revision'])
    first = images.generate_frame(pid, 1, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='frame1', revision=state['revision'])
    assert first['status'] == 'COMPLETED'
    assert images.image_path(pid, first['image_id']).read_bytes() == png()
    updated = images.confirm_frame(pid, 1, first['image_id'], workflow.get(pid)['revision'])
    assert updated['confirmed_frame_id'] == first['image_id']
    updated = workflow.edit_shot(pid, 1, 'A different river view beneath the trees.', updated['revision'])
    assert updated['shots'][0]['confirmed_frame_id'] is None
    assert images.list_frames(pid, 1)[0]['stale'] == 1


def test_selecting_video_from_current_frame_resolves_old_video_decision(tmp_path, monkeypatch):
    from studio import timeline
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'The river flows in a quiet forest.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    workflow.confirm_storyboard(pid, state['revision'])
    shot_id = core.storyboard(pid)['shots'][0]['id']
    video = tmp_path / 'video.mp4'; video.write_bytes(b'video')
    asset = timeline.import_asset(pid, 1, str(video))
    with core.connect() as db:
        db.execute('INSERT INTO guided_frame_selection VALUES (?,?,?)', (pid, shot_id, 'frame'))
        db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL)', (pid, shot_id))
        db.execute('INSERT INTO guided_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('attempt',pid,shot_id,'request','prompt',10,'mock',0,'COMPLETED',None,None,str(video),asset,None))
        db.execute('INSERT INTO guided_video_provenance(attempt_id,frame_id,frame_sha256,model) VALUES (?,?,?,?)',
                   ('attempt','frame','hash','mock'))
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


def test_confirming_different_frame_requires_review_of_old_video(tmp_path, monkeypatch):
    from studio import timeline
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    pid = core.create('Forest', 10, 'mock', 5)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'The river flows in a quiet forest.', 'duration': 10, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    monkeypatch.setattr(images, '_image_request', lambda *a, **kw: {'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    first = images.generate_frame(pid, 1, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='one', revision=state['revision'])
    images.confirm_frame(pid, 1, first['image_id'], workflow.get(pid)['revision'])
    video = tmp_path / 'old.mp4'; video.write_bytes(b'old')
    asset = timeline.import_asset(pid, 1, str(video)); timeline.select(pid, 1, asset)
    second = images.generate_frame(pid, 1, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='two', revision=workflow.get(pid)['revision'])
    images.confirm_frame(pid, 1, second['image_id'], workflow.get(pid)['revision'])
    assert workflow.get(pid)['shots'][0]['old_video_decision_required']


def test_unrelated_character_edit_during_frame_generation_does_not_mark_frame_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    pid = core.create('Two characters', 10, 'mock', 1)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'A', 'Elephant', 0)
    state = roles.save_character(pid, None, 'B', 'Parrot', state['revision'])
    a,b = [r['id'] for r in state['characters']]
    for cid in (a,b):
        img = images.import_character_image(pid, cid, 'role.png', png(), state['revision'])
        state = roles.confirm_character(pid, cid, img['id'], img['revision'])
    state = workflow.save_draft(pid, {'character_bible':{},'shots':[
        {'prompt':'An elephant walks in a quiet forest.', 'duration':10,'character_ids':[a]}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    def edit_unrelated(*args, **kwargs):
        roles.save_character(pid, b, 'B', 'A different parrot', workflow.get(pid)['revision'])
        return {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
    monkeypatch.setattr(images, '_image_edit', edit_unrelated)
    result = images.generate_frame(pid, 1, 'gpt-image-2.5-sunburst', 1, confirm_paid=True, request_id='frame', revision=state['revision'])
    assert result['status'] == 'COMPLETED'
    assert images.list_frames(pid, 1)[0]['stale'] == 0
