import pytest

from studio import core, guided_storyboard as gs
from studio.cli import app
from typer.testing import CliRunner


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return core.create('A small elephant explores a jungle', 20, 'mock', 5)


def test_preview_and_missing_authorization_make_no_calls(project, monkeypatch):
    def fail(*args):
        pytest.fail('paid request unexpectedly invoked')
    monkeypatch.setattr(core, '_llm_json', fail)
    assert gs.preview(project, 'test-model')['estimated_cost_usd'] is None
    for budget, confirm in [(0, True), (1, False), (float('nan'), True)]:
        with pytest.raises(ValueError):
            gs.request_plan(project, 'test-model', budget, confirm_paid=confirm)
    assert core.storyboard(project)['shots'] == []


def test_variable_plan_validates_and_does_not_save(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {
        'character_bible': {'hero': 'elephant'}, 'shots': [
            {'prompt': 'An elephant walks beneath the trees.', 'duration': 8},
            {'prompt': 'The elephant approaches a wide river.', 'duration': 12},
        ]})
    draft = gs.request_plan(project, 'test-model', 1, confirm_paid=True)
    assert [s['duration'] for s in draft['shots']] == [8, 12]
    assert core.storyboard(project)['shots'] == []


@pytest.mark.parametrize('shots', [[], [{'prompt': 'A long enough prompt', 'duration': 21}],
    [{'prompt': 'A long enough prompt', 'duration': 8}], [{'prompt': 'short', 'duration': 20}]])
def test_invalid_plan_does_not_save(project, monkeypatch, shots):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'character_bible': {}, 'shots': shots})
    with pytest.raises(ValueError):
        gs.request_plan(project, 'test-model', 1, confirm_paid=True)
    assert core.storyboard(project)['shots'] == []


def test_fixed_length_is_checked(project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *args: {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks beneath the trees.', 'duration': 8},
        {'prompt': 'The elephant approaches a wide river.', 'duration': 12}]})
    with pytest.raises(ValueError):
        gs.request_plan(project, 'test-model', 1, confirm_paid=True, shot_length=10)


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
