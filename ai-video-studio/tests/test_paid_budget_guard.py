import pytest
from studio import core, narration, reference_generation


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return core.create('A small elephant', 20, 'minimax', 5)


def test_cloud_generation_requires_explicit_run_budget(project, monkeypatch):
    monkeypatch.setattr(core, 'require_ffmpeg', lambda: None)
    with pytest.raises(ValueError, match='budget'):
        core.generate(project, live=True)


@pytest.mark.parametrize('operation', [
    lambda pid: narration.plan(pid, model='fake', live=True),
    lambda pid: narration.synthesize(pid, live=True),
    lambda pid: reference_generation.generate_frame(pid, 1, live=True),
])
def test_other_paid_operations_require_budget_before_side_effects(project, operation):
    with pytest.raises(ValueError, match='budget'):
        operation(project)


def test_mock_generation_does_not_delete_invalid_existing_media(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setattr(core, 'require_ffmpeg', lambda: None)
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: (_ for _ in ()).throw(ValueError('bad video')))
    pid = core.create('Old asset must be preserved', 10, 'mock', 0)
    core.plan(pid, 10)
    path = tmp_path / pid / 'clips' / f'{pid}-s001.mp4'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'old-asset')
    with pytest.raises(ValueError):
        core.generate(pid)
    assert path.read_bytes() == b'old-asset'


def test_legacy_render_does_not_overwrite_existing_final(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    monkeypatch.setattr(core, 'require_ffmpeg', lambda: None)
    pid = core.create('Keep the final film', 10, 'mock', 0)
    core.plan(pid, 10)
    with core.connect() as db:
        db.execute("UPDATE shots SET status='COMPLETED' WHERE project_id=?", (pid,))
    final = tmp_path / pid / 'final.mp4'
    final.parent.mkdir(parents=True)
    final.write_bytes(b'existing-final')
    with pytest.raises(ValueError, match='exists'):
        core.render(pid)
    assert final.read_bytes() == b'existing-final'
