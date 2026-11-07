import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from studio import core, guided_generation as generation, guided_workflow as workflow, timeline


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('An elephant explores a river', 20, 'mock', 4)
    workflow.create(pid)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant explores a forest.', 'duration': 10},
        {'prompt': 'An elephant approaches a river.', 'duration': 10}]}, 0)
    workflow.confirm_storyboard(pid, state['revision'])
    monkeypatch.setattr(core, 'mock_generate', lambda shot, path: path.write_bytes(b'generated'))
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    return pid, tmp_path


def test_preview_and_order_rejection_do_not_create_attempts(project):
    pid, _ = project
    assert generation.preview(pid, 1)['paid'] is False
    with pytest.raises(ValueError):
        generation.submit(pid, 2, 1, confirm_paid=True, request_id='second')
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_attempts').fetchone()[0] == 0


def test_mock_regeneration_preserves_versions_and_idempotency(project):
    pid, _ = project
    first = generation.submit(pid, 1, 0, confirm_paid=False, request_id='first')
    repeated = generation.submit(pid, 1, 0, confirm_paid=False, request_id='first')
    second = generation.submit(pid, 1, 0, confirm_paid=False, request_id='second')
    assert first['id'] == repeated['id']
    assert first['id'] != second['id']
    assert first['file'] != second['file']
    assert len(timeline.assets(pid, 1)) == 2
    assert open(first['file'], 'rb').read() == b'generated'


def test_paid_authorization_required_before_submission(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 2, confirm_paid=False, request_id='no')
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 0.5, confirm_paid=True, request_id='low')
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_attempts').fetchone()[0] == 0


def test_failed_submission_is_ambiguous_and_cannot_resubmit(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    calls = []
    class Provider:
        def __init__(self, client): pass
        def submit(self, prompt, duration):
            calls.append(prompt)
            raise OSError('network uncertain')
    monkeypatch.setitem(core.PROVIDERS, 'minimax', Provider)
    state = generation.submit(pid, 1, 2, confirm_paid=True, request_id='ambiguous')
    assert state['status'] == 'SUBMITTING'
    with pytest.raises(ValueError):
        generation.submit(pid, 1, 2, confirm_paid=True, request_id='different')
    assert len(calls) == 1


def test_concurrent_duplicate_request_submits_only_once(project, monkeypatch):
    pid, _ = project
    with core.connect() as db:
        db.execute("UPDATE projects SET provider='minimax' WHERE id=?", (pid,))
    monkeypatch.setenv('MINIMAX_USD_PER_SECOND', '0.1')
    entered, release = Event(), Event()
    calls = []
    class Provider:
        def __init__(self, client): pass
        def submit(self, prompt, duration):
            calls.append(prompt)
            entered.set()
            assert release.wait(5)
            return 'remote-1'
    monkeypatch.setitem(core.PROVIDERS, 'minimax', Provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(generation.submit, pid, 1, 2, confirm_paid=True, request_id='same')
        assert entered.wait(5)
        duplicate = pool.submit(generation.submit, pid, 1, 2, confirm_paid=True, request_id='same').result(timeout=5)
        assert duplicate['status'] == 'SUBMITTING'
        release.set()
        assert first.result(timeout=5)['status'] == 'SUBMITTED'
    assert len(calls) == 1
