"""Exercise real media; never let acceptance silently use a paid service."""
import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from studio import core, sound


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
    sentinel_home = tmp_path_factory.mktemp('real-home')
    sentinel_db = sentinel_home / 'studio.sqlite3'
    sentinel_db.write_bytes(b'sentinel-project-database')
    home, send = core.HOME, httpx.Client.send
    observations = []
    real_export = sound.export
    with ExitStack() as stack:
        stack.enter_context(patch.object(core, 'HOME', sentinel_home))
        generated = stack.enter_context(patch.object(core, 'mock_generate', wraps=core.mock_generate))
        for provider in set(core.PROVIDERS.values()):
            for name in ('submit', 'submit_i2v'):
                if hasattr(provider, name):
                    stack.enter_context(patch.object(provider, name, side_effect=AssertionError('Paid submission')))

        def observe_export(*args, **kwargs):
            result = real_export(*args, **kwargs)
            observations.append(generated.call_count)
            return result

        stack.enter_context(patch.object(sound, 'export', observe_export))
        report_path = runner.run_baseline(root)
    assert core.HOME == home
    assert httpx.Client.send is send
    assert sentinel_db.read_bytes() == b'sentinel-project-database'
    report = json.loads(report_path.read_text())
    report['test_generation_calls_at_exports'] = observations
    return root, report


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
    assert report['metrics']['blocked_http_attempts'] == 0
    checks = {c['id']: c['status'] for c in report['checks']}
    assert checks['no_external_http'] == 'passed'
    assert checks['human_viewing'] == 'not_verified'
    for name in ('baseline', 'shot_replacement', 'subtitle_revision'):
        assert checks[f'{name}_video'] == 'passed'
        assert checks[f'{name}_render_manifest_matches_selections'] == 'passed'
        data = report['artifacts'][name]
        assert (root / data['manifest']['path']).is_file()
        assert (root / data['render_manifest']['path']).is_file()
        shots = json.loads((root / data['render_manifest']['path']).read_text())['shots']
        assert [{'seq': s['seq'], 'asset_id': s['asset_id']} for s in shots] == data['selections']
        assert json.loads((root / data['manifest']['path']).read_text())['config']['subtitles'] \
            == str((root / data['subtitles']['path']).resolve())


def _artifact_paths(value):
    if isinstance(value, dict):
        if 'path' in value and 'sha256' in value:
            yield value
        for child in value.values():
            yield from _artifact_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifact_paths(child)


def test_all_reported_artifacts_match_hashes(baseline_bundle):
    root, report = baseline_bundle
    artifacts = list(_artifact_paths(report['artifacts']))
    assert len(artifacts) >= 15
    for item in artifacts:
        assert hashlib.sha256((root / item['path']).read_bytes()).hexdigest() == item['sha256']


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


def test_truncated_media_cannot_pass_video_check(tmp_path):
    runner = load_runner()
    video = tmp_path / 'truncated.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=24:d=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '4', str(video),
    ], check=True)
    payload = video.read_bytes()
    video.write_bytes(payload[:len(payload) // 10])
    probe = runner.evidence.inspect_video(video)
    assert probe['decode_ok'] is False
    assert runner._video_ok(probe) is False


def test_report_write_failure_preserves_original_error(tmp_path, monkeypatch):
    runner = load_runner()
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(runner.evidence, 'write_bundle_report',
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(RuntimeError, match='ffmpeg'):
        runner.run_baseline(tmp_path / 'bundle')


def test_local_edits_reuse_other_video_assets(baseline_bundle):
    root, report = baseline_bundle
    artifacts = report['artifacts']
    assert 'shot_replacement' in artifacts, 'local replacement scenario not implemented'
    assert 'subtitle_revision' in artifacts, 'subtitle revision scenario not implemented'
    original, replaced, revised = (artifacts[n] for n in ('baseline', 'shot_replacement', 'subtitle_revision'))
    assert report['test_generation_calls_at_exports'] == [6, 6, 6]
    assert len({r['video']['path'] for r in (original, replaced, revised)}) == 3
    assert all((root / r['video']['path']).is_file() for r in (original, replaced, revised))
    assert [s['seq'] for s in original['selections']] == [1, 2, 3, 4, 5, 6]
    changed = [a['seq'] for a, b in zip(original['selections'], replaced['selections'])
               if a['asset_id'] != b['asset_id']]
    assert changed == [3]
    assert revised['selections'] == replaced['selections']
    for source in artifacts['source_videos']:
        assert hashlib.sha256((root / source['path']).read_bytes()).hexdigest() == source['sha256']
    assert replaced['subtitles']['sha256'] != revised['subtitles']['sha256']
    for key in ('narration', 'music', 'ambience', 'narration_gain', 'music_gain',
                'ambience_gain', 'fade_seconds'):
        assert revised['soundtrack'][key] == replaced['soundtrack'][key]
    hashes = []
    for version in (replaced, revised):
        pcm = subprocess.check_output([
            'ffmpeg', '-v', 'error', '-i', str(root / version['video']['path']),
            '-map', '0:a:0', '-ac', '2', '-ar', '48000', '-f', 's16le', '-',
        ])
        hashes.append(hashlib.sha256(pcm).hexdigest())
        assert version['audio']['mean_volume_db'] > -60
    assert hashes[0] == hashes[1]
    checks = {c['id']: c['status'] for c in report['checks']}
    for name in ('source_assets_unchanged', 'single_shot_replaced',
                 'subtitle_edit_video_generation_calls_zero'):
        assert checks[name] == 'passed'
