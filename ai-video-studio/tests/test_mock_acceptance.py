"""Exercise real media; never let acceptance silently use a paid service."""
import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from studio import core


def load_runner():
    path = Path(__file__).parents[1] / 'scripts' / 'mock_acceptance.py'
    assert path.is_file(), 'mock acceptance runner not implemented'
    spec = importlib.util.spec_from_file_location('mock_acceptance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def baseline_bundle(tmp_path_factory):
    runner = load_runner()
    root = tmp_path_factory.mktemp('acceptance-parent') / 'bundle'
    home, send = core.HOME, httpx.Client.send
    report_path = runner.run_baseline(root)
    assert core.HOME == home
    assert httpx.Client.send is send
    return root, json.loads(report_path.read_text())


def test_existing_output_root_is_rejected(tmp_path):
    runner = load_runner()
    marker = tmp_path / 'sentinel'
    marker.write_text('leave me')
    home = core.HOME
    with pytest.raises(FileExistsError):
        runner.run_baseline(tmp_path)
    assert marker.read_text() == 'leave me'
    assert list(tmp_path.iterdir()) == [marker]
    assert core.HOME == home


def test_60_second_baseline_bundle(baseline_bundle):
    root, report = baseline_bundle
    sources = report['artifacts']['source_videos']
    assert len(sources) == 6
    for source in sources:
        assert source['probe']['width'] == 640
        assert source['probe']['height'] == 360
        assert source['sha256'] == hashlib.sha256((root / source['path']).read_bytes()).hexdigest()
    baseline = report['artifacts']['baseline']
    video = root / baseline['video']['path']
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(video),
    ]))
    assert abs(float(probe['format']['duration']) - 60) <= 0.25
    stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert (stream['width'], stream['height'], stream['avg_frame_rate']) == (1280, 720, '24/1')
    assert any(s['codec_type'] == 'audio' for s in probe['streams'])
    audio = subprocess.run([
        'ffmpeg', '-hide_banner', '-i', str(video), '-vn', '-af', 'volumedetect', '-f', 'null', '-',
    ], capture_output=True, text=True, check=True)
    mean_db = float(re.search(r'mean_volume:\s*([-\d.]+) dB', audio.stderr).group(1))
    assert mean_db > -60
    srt = (root / baseline['subtitles']['path']).read_text()
    assert srt.count(' --> ') == 6
    assert '00:00:00,000 --> 00:00:10,000' in srt
    assert '00:00:50,000 --> 00:01:00,000' in srt
    manifest = json.loads((root / baseline['manifest']['path']).read_text())
    assert manifest['burn_subtitles'] is True
    assert manifest['config']['narration']
    assert manifest['config']['music']
    assert report['artifacts']['audio_inputs']['kind'] == 'synthetic_placeholder'
    assert report['costs'] == {'external_api_calls': 0, 'estimated_usd': 0,
                               'actual_usd': None, 'billing_status': 'not_applicable'}
    assert set(report['not_verified']) >= {
        'real_provider', 'real_tts', 'one_click_workflow', 'worker_restart_recovery', 'provider_billing',
    }
    assert report['metrics']['video_generation_calls'] == 6
    assert report['metrics']['required_clicks'] is None
    assert report['metrics']['elapsed_sec'] > 0
    assert all(c['status'] != 'failed' for c in report['checks'])


@pytest.mark.parametrize('asynchronous', [False, True])
def test_accidental_http_is_blocked_and_failure_is_recorded(tmp_path, monkeypatch, asynchronous):
    runner = load_runner()
    home, send, async_send = core.HOME, httpx.Client.send, httpx.AsyncClient.send

    def accidental_network(*args, **kwargs):
        if asynchronous:
            async def request():
                async with httpx.AsyncClient() as client:
                    await client.get('https://must-not-contact.invalid')
            asyncio.run(request())
        else:
            with httpx.Client() as client:
                client.get('https://must-not-contact.invalid')

    monkeypatch.setattr(core, 'generate', accidental_network)
    root = tmp_path / 'bundle'
    with pytest.raises(AssertionError, match='HTTP disabled'):
        runner.run_baseline(root)
    report = json.loads((root / 'report.json').read_text())
    assert any(c['status'] == 'failed' for c in report['checks'])
    assert 'baseline' not in report['artifacts']
    assert core.HOME == home
    assert httpx.Client.send is send
    assert httpx.AsyncClient.send is async_send


def test_missing_tools_leave_failure_evidence(tmp_path, monkeypatch):
    runner = load_runner()
    monkeypatch.setenv('PATH', '')
    root = tmp_path / 'bundle'
    home = core.HOME
    with pytest.raises(RuntimeError, match='ffmpeg'):
        runner.run_baseline(root)
    report = json.loads((root / 'report.json').read_text())
    assert report['environment']['ffmpeg'] is None
    assert report['environment']['errors']
    assert any(c['status'] == 'failed' for c in report['checks'])
    assert core.HOME == home
