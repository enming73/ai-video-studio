"""Evidence must distinguish playable fixtures, damaged files and unverified claims."""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def evidence_module():
    path = Path(__file__).parents[1] / 'scripts' / 'acceptance_evidence.py'
    assert path.is_file(), 'acceptance evidence module not implemented'
    spec = importlib.util.spec_from_file_location('acceptance_evidence', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report_fixture():
    return {
        'schema_version': 1, 'mode': 'mock_baseline', 'project_id': 'fixture',
        'source': {'commit': 'fixture-commit', 'dirty': False},
        'environment': {'python': '3.11', 'ffmpeg': 'fixture', 'ffprobe': 'fixture'},
        'artifacts': {},
        'checks': [{'id': 'human_viewing', 'status': 'not_verified',
                    'evidence': 'Requires complete viewing by a person'}],
        'costs': {'external_api_calls': 0, 'estimated_usd': 0,
                  'actual_usd': None, 'billing_status': 'not_applicable'},
        'metrics': {'elapsed_sec': 0.0, 'video_generation_calls': 0,
                    'required_clicks': None, 'measurement_scope': 'legacy_script'},
        'manual_steps': [],
        'not_verified': ['real_provider', 'real_tts', 'one_click_workflow',
                         'worker_restart_recovery', 'provider_billing'],
    }


def test_valid_video_has_probe_and_hash(evidence_module, tmp_path):
    video = tmp_path / 'fixture.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=24:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '1', str(video),
    ], check=True)
    result = evidence_module.inspect_video(video)
    assert result['readable'] is True
    assert abs(result['duration_sec'] - 1) <= 0.25
    assert (result['width'], result['height'], result['fps']) == (320, 180, 24)
    assert result['has_audio'] is True
    assert result['sha256'] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert result['error'] is None


def test_invalid_media_is_not_passed(evidence_module, tmp_path):
    video = tmp_path / 'broken.mp4'
    video.write_text('not a video')
    result = evidence_module.inspect_video(video)
    assert result['readable'] is False
    assert result['duration_sec'] is None
    assert result['error']
    missing = evidence_module.inspect_video(tmp_path / 'missing.mp4')
    assert missing['readable'] is False
    assert missing['sha256'] is None


def test_report_preserves_unknowns_and_refuses_overwrite(evidence_module, tmp_path):
    evidence = report_fixture()
    target = evidence_module.write_bundle_report(tmp_path, evidence)
    saved = target.read_bytes()
    report = json.loads(saved)
    assert report['costs']['actual_usd'] is None
    assert report['metrics']['required_clicks'] is None
    assert 'real_tts' in report['not_verified']
    assert report['checks'][0]['status'] == 'not_verified'
    with pytest.raises(FileExistsError):
        evidence_module.write_bundle_report(tmp_path, evidence)
    assert target.read_bytes() == saved


@pytest.mark.parametrize('change', ['missing_source', 'empty_environment', 'bad_status',
                                   'missing_unknowns', 'fabricated_bill', 'absolute_path'])
def test_report_rejects_incomplete_or_misleading_evidence(evidence_module, tmp_path, change):
    report = report_fixture()
    if change == 'missing_source':
        del report['source']
    elif change == 'empty_environment':
        report['environment'] = {}
    elif change == 'bad_status':
        report['checks'][0]['status'] = 'probably_passed'
    elif change == 'missing_unknowns':
        report['not_verified'] = []
    elif change == 'fabricated_bill':
        report['costs']['actual_usd'] = 0
    else:
        report['artifacts'] = {'video': {'path': '/outside/private.mp4'}}
    with pytest.raises(ValueError):
        evidence_module.write_bundle_report(tmp_path, report)
    assert not (tmp_path / 'report.json').exists()
