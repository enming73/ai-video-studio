import pytest

from studio import core, request_jobs


@pytest.fixture
def job_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return core.create('A river', 10, 'minimax', 5)


def test_request_job_reuses_unresolved_target(job_project):
    pid = job_project
    first = request_jobs.submit(pid, kind='image', request_id='a', target_key='character:c1',
                                payload={'x': 1}, budget_usd=1)
    second = request_jobs.submit(pid, kind='image', request_id='b', target_key='character:c1',
                                 payload={'x': 1}, budget_usd=1)
    assert first['id'] == second['id']
    assert request_jobs.find(pid, kind='image', request_id='b')['id'] == first['id']


def test_request_job_rejects_invalid_budget(job_project):
    pid = job_project
    for budget in (0, -1, float('nan'), float('inf'), True):
        with pytest.raises(ValueError):
            request_jobs.submit(pid, kind='image', request_id='x', target_key='character:c1',
                                payload={'x': 1}, budget_usd=budget)


def test_request_job_idempotent_by_request_id(job_project):
    pid = job_project
    first = request_jobs.submit(pid, kind='image', request_id='same', target_key='character:c1',
                                payload={'x': 1}, budget_usd=1)
    replay = request_jobs.submit(pid, kind='image', request_id='same', target_key='character:c1',
                                 payload={'x': 1}, budget_usd=1)
    assert replay['id'] == first['id']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


def test_request_id_belongs_to_another_operation(job_project):
    pid = job_project
    request_jobs.submit(pid, kind='image', request_id='x', target_key='character:c1',
                        payload={'x': 1}, budget_usd=1)
    with pytest.raises(ValueError, match='another operation'):
        request_jobs.submit(pid, kind='image', request_id='x', target_key='character:c2',
                            payload={'x': 1}, budget_usd=1)


def test_find_returns_none_for_unknown(job_project):
    assert request_jobs.find(job_project, kind='image', request_id='missing') is None


def test_latest_attempt_returns_none_without_attempts(job_project):
    pid = job_project
    job = request_jobs.submit(pid, kind='image', request_id='a', target_key='character:c1',
                              payload={'x': 1}, budget_usd=1)
    assert request_jobs.latest_attempt(job) is None
