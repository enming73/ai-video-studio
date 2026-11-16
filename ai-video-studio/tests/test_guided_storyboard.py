import json

import pytest

from studio import core, guided_storyboard as gs
from studio import guided_compat, jobs, worker
from studio.cli import app
from typer.testing import CliRunner


DRAFT = {'character_bible': {}, 'shots': [
    {'prompt': 'A river flows through the forest.', 'duration': 4},
    {'prompt': 'The river reaches a wide clearing.', 'duration': 6}]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return core.create('A small elephant explores a jungle', 20, 'mock', 5)


@pytest.fixture
def guided_storyboard_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A river', 10, 'mock', 5)
    from studio import guided_workflow, request_jobs
    guided_workflow.create(pid)
    gs.request_plan(pid, 'test-model', 1, confirm_paid=True, request_id='new', revision=0)
    return request_jobs.find(pid, kind='llm_storyboard', request_id='new')


def _payload(project, **overrides):
    details = gs.preview(project, 'test-model')
    with core.connect() as db:
        p = dict(core.project(db, project))
        character_stage = bool(db.execute(
            'SELECT 1 FROM guided_character_settings WHERE project_id=?', (project,)).fetchone())
    payload = {
        'brief': p['prompt'], 'model': 'test-model',
        'duration': details['duration'],
        'allowed_durations': details['allowed_durations'],
        'fixed_durations': details['fixed_durations'],
        'character_stage': character_stage,
    }
    payload.update(overrides)
    return payload


def test_preview_and_missing_authorization_make_no_calls(project, monkeypatch):
    def fail(*args):
        pytest.fail('paid request unexpectedly invoked')
    monkeypatch.setattr(core, '_llm_json', fail)
    assert gs.preview(project, 'test-model')['estimated_cost_usd'] is None
    for budget, confirm in [(0, True), (1, False), (float('nan'), True)]:
        with pytest.raises(ValueError):
            gs.request_plan(project, 'test-model', budget, confirm_paid=confirm,
                            request_id='x', revision=0)
    assert core.storyboard(project)['shots'] == []


def test_request_plan_enqueues_without_llm_call(project, monkeypatch):
    from studio import guided_workflow, request_jobs
    guided_workflow.create(project)
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    result = gs.request_plan(project, 'test-model', 1, confirm_paid=True,
                             request_id='job', revision=0)
    assert result['status'] == 'QUEUED'
    assert result['request_id'] == 'job'
    assert request_jobs.find(project, kind='llm_storyboard', request_id='job') is not None
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_planning_attempts').fetchone()[0] == 0


def test_variable_plan_validates_and_does_not_save(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {
        'character_bible': {'hero': 'elephant'}, 'shots': [
            {'prompt': 'An elephant walks beneath the trees.', 'duration': 8},
            {'prompt': 'The elephant approaches a wide river.', 'duration': 12},
        ]})
    draft = gs.generate_draft(_payload(project))
    assert [s['duration'] for s in draft['shots']] == [8, 12]
    assert core.storyboard(project)['shots'] == []


@pytest.mark.parametrize('shots', [[], [{'prompt': 'A long enough prompt', 'duration': 21}],
    [{'prompt': 'A long enough prompt', 'duration': 8}], [{'prompt': 'short', 'duration': 20}]])
def test_invalid_plan_does_not_save(project, monkeypatch, shots):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'character_bible': {}, 'shots': shots})
    with pytest.raises(ValueError):
        gs.generate_draft(_payload(project))
    assert core.storyboard(project)['shots'] == []


def test_fixed_length_is_checked(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks beneath the trees.', 'duration': 8},
        {'prompt': 'The elephant approaches a wide river.', 'duration': 12}]})
    with pytest.raises(ValueError):
        gs.generate_draft(_payload(project, fixed_durations=[10, 10]))


def test_cli_smart_plan_defaults_to_dry_run(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    result = CliRunner().invoke(app, ['smart-plan', project, '--model', 'test-model'])
    assert result.exit_code == 0
    assert 'DRY' in result.output.upper()
    assert core.storyboard(project)['shots'] == []


def test_cli_live_requires_budget(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    result = CliRunner().invoke(app, ['smart-plan', project, '--model', 'test-model', '--live'])
    assert result.exit_code != 0
    assert core.storyboard(project)['shots'] == []


def test_legacy_core_smart_plan_without_budget_never_calls_llm(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    with pytest.raises(ValueError):
        core.smart_plan(project, 10, 'test-model')


def test_cli_cannot_submit_legacy_plan_for_guided_project(project, monkeypatch):
    from studio import guided_workflow
    guided_workflow.create(project)
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    result = CliRunner().invoke(app, ['smart-plan', project, '--model', 'test', '--live', '--budget', '1'])
    assert result.exit_code != 0


def test_fixed_length_zero_is_rejected_without_paid_call(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: pytest.fail('unexpected paid call'))
    with pytest.raises(ValueError):
        gs.preview(project, 'test-model', 0)


def test_fixed_length_preserves_valid_short_final_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A small elephant', 25, 'mock', 3)
    preview = gs.preview(pid, 'model', 10)
    assert preview['fixed_durations'] == [10, 10, 5]


def test_character_enabled_plan_includes_cast_suggestions(project, monkeypatch):
    from studio import guided_workflow
    guided_workflow.create(project, character_stage_enabled=True)
    monkeypatch.setattr(core, '_llm_json', lambda *args: {
        'character_bible': {'Luma': 'gray elephant', 'Pico': 'blue parrot'},
        'shots': [{'prompt': 'Luma walks through a dense forest.', 'duration': 8, 'character_names': ['Luma']},
                  {'prompt': 'Pico flies above the clear river.', 'duration': 12, 'character_names': ['Pico']}]})
    draft = gs.generate_draft(_payload(project))
    assert {c['name'] for c in draft['characters']} == {'Luma', 'Pico'}
    ids = {c['name']: c['id'] for c in draft['characters']}
    assert draft['shots'][0]['character_ids'] == [ids['Luma']]
    assert draft['shots'][1]['character_ids'] == [ids['Pico']]


def test_character_enabled_plan_rejects_unknown_cast(project, monkeypatch):
    from studio import guided_workflow
    guided_workflow.create(project, character_stage_enabled=True)
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'character_bible': {'Luma': 'elephant'},
        'shots': [{'prompt': 'An elephant walks in the deep forest.', 'duration': 20, 'character_names': ['unknown']}]})
    with pytest.raises(ValueError, match='character'):
        gs.generate_draft(_payload(project))


def test_plan_attempts_merge_legacy_and_jobs(guided_storyboard_job):
    pid = guided_storyboard_job['project_id']
    with core.connect() as db:
        db.execute('INSERT INTO guided_planning_attempts VALUES (?,?,?,?,?,?,?)',
                   (pid, 'old', 'UNKNOWN', 'm', 1, None, 'old failure'))
    rows = guided_compat.plan_attempt_records(pid)
    assert [r['request_id'] for r in rows] == ['new', 'old']
    assert set(rows[0]) == {'project_id', 'request_id', 'status', 'model', 'budget_usd', 'result_json', 'error'}


def test_storyboard_worker_saves_draft_and_completes(guided_storyboard_job, monkeypatch):
    from studio.handlers import register_all
    register_all()
    pid = guided_storyboard_job['project_id']
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setattr(gs, 'generate_draft', lambda payload: DRAFT)
    assert worker.run_once() == guided_storyboard_job['id']
    assert guided_compat.find_plan(pid, 'new')['status'] == 'COMPLETED'
    from studio import guided_workflow
    assert guided_workflow.get(pid)['draft'] == DRAFT
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_planning_attempts').fetchone()[0] == 0


def test_storyboard_job_rejects_stale_revision_without_overwrite(guided_storyboard_job, monkeypatch):
    from studio.handlers import register_all
    register_all()
    pid = guided_storyboard_job['project_id']
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setattr(gs, 'generate_draft', lambda payload: DRAFT)
    with core.connect() as db:
        db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?', (pid,))
    assert worker.run_once() == guided_storyboard_job['id']
    assert jobs.get(guided_storyboard_job['id'])['status'] == 'FAILED'
    from studio import guided_workflow
    assert guided_workflow.get(pid)['draft'] is None


def test_storyboard_missing_key_fails_without_attempt_or_cost(guided_storyboard_job, monkeypatch):
    from studio.handlers import register_all
    register_all()
    pid = guided_storyboard_job['project_id']
    monkeypatch.delenv('STUDIO_LLM_API_KEY', raising=False)
    monkeypatch.setattr(core, '_llm_json', lambda *a: pytest.fail('no request without a key'))
    assert worker.run_once() == guided_storyboard_job['id']
    assert jobs.get(guided_storyboard_job['id'])['status'] == 'FAILED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM job_attempts WHERE job_id=?',
                          (guided_storyboard_job['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM cost_entries WHERE job_id=?',
                          (guided_storyboard_job['id'],)).fetchone()[0] == 0


def test_storyboard_invalid_response_marks_failed_final_and_reserves(guided_storyboard_job, monkeypatch):
    from studio.handlers import register_all
    register_all()
    pid = guided_storyboard_job['project_id']
    monkeypatch.setenv('STUDIO_LLM_API_KEY', 'fake')
    monkeypatch.setattr(core, '_llm_json', lambda *a: {})
    assert worker.run_once() == guided_storyboard_job['id']
    assert jobs.get(guided_storyboard_job['id'])['status'] == 'FAILED'
    with core.connect() as db:
        attempt = db.execute('SELECT status FROM job_attempts WHERE job_id=?',
                             (guided_storyboard_job['id'],)).fetchone()
        assert attempt['status'] == 'FAILED_FINAL'
        entry = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                           (guided_storyboard_job['id'],)).fetchone()
        assert entry['billing_status'] == 'reserved'


def test_resolve_plan_requires_confirmation_and_acknowledges(guided_storyboard_job):
    from studio import attempts
    job = guided_storyboard_job
    pid = job['project_id']
    attempt = attempts.create(job['id'], provider='llm', model='test-model',
                              input_data=json.loads(job['payload_json']))
    attempts.prepare_submission(attempt['id'], amount_microusd=1_000_000, pricing_version='test:v1')
    attempts.mark_unknown(attempt['id'], 'uncertain submission')
    with core.connect() as db:
        db.execute("UPDATE jobs SET status='BLOCKED',block_reason='unknown_remote' WHERE id=?",
                   (job['id'],))
    with pytest.raises(ValueError, match='confirmation'):
        gs.resolve_plan(pid, 'new', confirm_checked=False)
    result = gs.resolve_plan(pid, 'new', confirm_checked=True)
    assert result['status'] == 'ACKNOWLEDGED'
    with core.connect() as db:
        assert jobs.get(job['id'], db=db)['status'] == 'CANCELLED'
        assert db.execute('SELECT billing_status FROM cost_entries WHERE attempt_id=?',
                          (attempt['id'],)).fetchone()[0] == 'reserved'
