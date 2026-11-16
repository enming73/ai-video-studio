"""Offline legacy media baseline. Run in its own process, never inside the API."""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from studio import core, sound, timeline


_spec = importlib.util.spec_from_file_location(
    'acceptance_evidence', Path(__file__).with_name('acceptance_evidence.py'))
evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence)
REPO = Path(__file__).resolve().parents[2]


def _command(args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=180)


def _identity():
    source = {'commit': None, 'dirty': None, 'errors': []}
    environment = {'python': platform.python_version(), 'ffmpeg': None,
                   'ffprobe': None, 'errors': []}
    try:
        source['commit'] = _command(['git', '-C', str(REPO), 'rev-parse', 'HEAD']).stdout.strip()
        source['dirty'] = bool(_command(['git', '-C', str(REPO), 'status', '--porcelain']).stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        source['errors'].append(type(exc).__name__ + ': git identity unavailable')
    for tool in ('ffmpeg', 'ffprobe'):
        try:
            environment[tool] = _command([tool, '-version']).stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError) as exc:
            environment['errors'].append(type(exc).__name__ + f': {tool} unavailable')
    return source, environment


def _check(report, name, passed, details):
    report['checks'].append({'id': name, 'status': 'passed' if passed else 'failed',
                             'evidence': details})
    if not passed:
        raise RuntimeError(f'Acceptance check failed: {name}')


def _file(root, path):
    return {'path': str(path.resolve().relative_to(root)),
            'sha256': evidence.file_hash(path)}


def _video(root, path):
    result = _file(root, path)
    result['probe'] = evidence.inspect_video(path)
    return result


def _video_ok(probe):
    return bool(probe['readable'] and probe['decode_ok']
                and probe['duration_sec'] is not None
                and abs(probe['duration_sec'] - 60) <= 0.25
                and (probe['width'], probe['height'], probe['fps']) == (1280, 720, 24)
                and probe['has_audio'])


def _captions(path, revised=False):
    blocks = []
    for seq in range(1, 7):
        start, end = (seq - 1) * 10, seq * 10
        blocks.append(
            f'{seq}\n{sound._srt_timestamp(start)} --> {sound._srt_timestamp(end)}\n'
            f'M0 {"revised" if revised else "baseline"} - shot {seq} - synthetic media\n')
    path.write_text('\n'.join(blocks), encoding='utf-8')


def _audio_evidence(path):
    proc = _command(['ffmpeg', '-hide_banner', '-i', str(path), '-vn',
                     '-af', 'volumedetect', '-f', 'null', '-'])
    match = re.search(r'mean_volume:\s*(-?[\d.]+|-inf) dB', proc.stderr)
    if match is None:
        raise RuntimeError('No audio volume measurement')
    value = float(match.group(1))
    # JSON has no infinity; None means silence/unavailable, never a pass.
    pcm_hash = _command([
        'ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0', '-ac', '2',
        '-ar', '48000', '-c:a', 'pcm_s16le', '-f', 'hash', '-hash', 'sha256', '-',
    ]).stdout.strip().removeprefix('SHA256=')
    return {'mean_volume_db': value if value != float('-inf') else None,
            'pcm_sha256': pcm_hash, 'pcm_format': 's16le/48000/stereo'}


def _render(root, pid, report, name, subtitles):
    output = sound.export(pid, source='selected', burn_subtitles=True)
    video = _video(root, output)
    audio = _audio_evidence(output)
    manifest = output.with_suffix('.json')
    data = json.loads(manifest.read_text())
    configured_subtitles = data['config'].get('subtitles')
    config = dict(data['config'])
    for key in ('narration', 'music', 'ambience', 'subtitles'):
        if config.get(key):
            config[key] = _file(root, Path(config[key]))
    selections = [{'seq': item['seq'], 'asset_id': item['selected']['id']}
                  for item in timeline.timeline(pid)]
    with core.connect() as db:
        render_row = db.execute(
            'SELECT output,manifest_json FROM render_history WHERE project_id=? '
            'ORDER BY rowid DESC LIMIT 1', (pid,)).fetchone()
    if render_row is None:
        raise RuntimeError('Timeline render manifest missing')
    render_file = Path(render_row['output']).with_suffix('.json')
    render_data = json.loads(render_row['manifest_json'])
    render_selections = [{'seq': shot['seq'], 'asset_id': shot['asset_id']}
                         for shot in render_data['shots']]
    report['artifacts'][name] = {
        'video': video, 'audio': audio, 'subtitles': _file(root, subtitles),
        'manifest': _file(root, manifest), 'render_manifest': _file(root, render_file),
        'soundtrack': config, 'selections': selections,
    }
    probe = video['probe']
    _check(report, name + '_video', _video_ok(probe), probe)
    _check(report, name + '_render_manifest_matches_selections',
           render_selections == selections,
           {'rendered': render_selections, 'selected': selections})
    _check(report, name + '_audible', audio['mean_volume_db'] is not None
           and audio['mean_volume_db'] > -60, audio)
    _check(report, name + '_subtitle_configuration',
           data['burn_subtitles'] is True
           and configured_subtitles is not None
           and Path(configured_subtitles).expanduser().resolve() == subtitles.resolve()
           and subtitles.read_text().count(' --> ') == 6,
           {'subtitle_path': str(subtitles.relative_to(root)),
            'configured_subtitles': configured_subtitles,
            'limitation': 'Configuration only; human readability not verified'})
    return output


def run_baseline(output_root: Path) -> Path:
    """Exclusive output directory; no remote calls; restores all process patches."""
    output_root = Path(output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=False)
    root = output_root.resolve()
    started = time.monotonic()
    source, environment = _identity()
    report = {
        'schema_version': 1, 'mode': 'mock_baseline', 'project_id': None,
        'source': source, 'environment': environment, 'artifacts': {},
        'checks': [{'id': 'human_viewing', 'status': 'not_verified',
                    'evidence': 'Complete viewing/listening by a person required'}],
        'costs': {'external_api_calls': 0, 'estimated_usd': 0, 'actual_usd': None,
                  'billing_status': 'not_applicable'},
        'metrics': {'elapsed_sec': 0, 'video_generation_calls': 0, 'required_clicks': None,
                    'measurement_scope': 'legacy_script', 'blocked_http_attempts': 0},
        'manual_steps': [
            {'action': 'Launch legacy baseline script', 'scope': 'operator'},
            {'action': 'Watch/listen to all exports', 'status': 'pending'},
        ],
        'not_verified': [*evidence.NOT_VERIFIED, 'human_viewing', 'guided_browser_clicks'],
    }

    def block_http(*args, **kwargs):
        report['metrics']['blocked_http_attempts'] += 1
        raise AssertionError('HTTP disabled during local acceptance')

    async def block_async_http(*args, **kwargs):
        block_http()

    stage = 'preflight'
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, 'HOME', root / 'studio-data'))
            stack.enter_context(patch.object(core, 'load_dotenv', return_value=False))
            stack.enter_context(patch.object(httpx.Client, 'send', block_http))
            stack.enter_context(patch.object(httpx.AsyncClient, 'send', block_async_http))
            generated = stack.enter_context(patch.object(core, 'mock_generate', wraps=core.mock_generate))
            try:
                if environment['ffmpeg'] is None or environment['ffprobe'] is None:
                    raise RuntimeError('ffmpeg and ffprobe are required')
                filters = _command(['ffmpeg', '-hide_banner', '-filters']).stdout
                _check(report, 'subtitle_filter_available', ' subtitles ' in filters,
                       'FFmpeg subtitles filter must be available')
                stage = 'generation'
                pid = core.create('M0 offline six-shot media baseline', 60, 'mock', 0)
                report['project_id'] = pid
                core.plan(pid, 10)
                core.generate(pid)
                timeline.sync(pid)
                items = timeline.timeline(pid)
                _check(report, 'six_selected_shots', len(items) == 6
                       and all(item['selected'] for item in items), {'count': len(items)})
                report['artifacts']['source_videos'] = [
                    dict(_video(root, Path(item['selected']['file'])),
                         seq=item['seq'], asset_id=item['selected']['id']) for item in items
                ]
                stage = 'audio_and_subtitles'
                narration, music = root / 'narration.wav', root / 'music.wav'
                for path, frequency in ((narration, 440), (music, 220)):
                    _command(['ffmpeg', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                              f'sine=frequency={frequency}:duration=60:sample_rate=48000',
                              '-ac', '2', '-c:a', 'pcm_s16le', str(path)])
                report['artifacts']['audio_inputs'] = {
                    'kind': 'synthetic_placeholder', 'narration': _file(root, narration),
                    'music': _file(root, music),
                }
                subtitles = root / 'baseline.srt'
                _captions(subtitles)
                sound.configure(pid, narration=str(narration), music=str(music),
                                subtitles=str(subtitles))
                stage = 'baseline_export'
                _render(root, pid, report, 'baseline', subtitles)
                stage = 'shot_replacement'
                before_edits = generated.call_count
                replacement = root / 'replacement.mp4'
                _command([
                    'ffmpeg', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                    'color=c=yellow:s=640x360:r=24:d=10',
                    '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(replacement),
                ])
                asset_id = timeline.import_asset(pid, 3, str(replacement),
                                                 source='acceptance-replacement')
                timeline.select(pid, 3, asset_id)
                report['artifacts']['replacement_input'] = _video(root, replacement)
                _render(root, pid, report, 'shot_replacement', subtitles)
                initial = report['artifacts']['baseline']['selections']
                replaced = report['artifacts']['shot_replacement']['selections']
                changed = [a['seq'] for a, b in zip(initial, replaced)
                           if a['asset_id'] != b['asset_id']]
                _check(report, 'single_shot_replaced',
                       len(initial) == len(replaced) == 6 and changed == [3],
                       {'changed_shots': changed})
                _check(report, 'replacement_video_generation_calls_zero',
                       generated.call_count == before_edits,
                       {'calls': generated.call_count - before_edits})
                stage = 'subtitle_revision'
                before_subtitles = generated.call_count
                revised_subtitles = root / 'revised.srt'
                _captions(revised_subtitles, revised=True)
                config = sound.config(pid)
                # configure replaces the full config. Preserve all audio settings.
                config.pop('project_id')
                config['subtitles'] = str(revised_subtitles)
                sound.configure(pid, **config)
                _render(root, pid, report, 'subtitle_revision', revised_subtitles)
                revised = report['artifacts']['subtitle_revision']
                replaced_export = report['artifacts']['shot_replacement']
                _check(report, 'subtitle_edit_video_generation_calls_zero',
                       generated.call_count == before_subtitles,
                       {'calls': generated.call_count - before_subtitles})
                _check(report, 'subtitle_edit_preserves_selections',
                       revised['selections'] == replaced,
                       {'selections': revised['selections']})
                _check(report, 'subtitle_edit_preserves_audio',
                       revised['audio']['pcm_sha256'] == replaced_export['audio']['pcm_sha256']
                       and all(revised['soundtrack'][k] == v for k, v in
                               replaced_export['soundtrack'].items() if k != 'subtitles'),
                       {'before': replaced_export['audio'], 'after': revised['audio']})
                _check(report, 'subtitle_version_changed',
                       revised['subtitles']['sha256'] != replaced_export['subtitles']['sha256'],
                       {'before': replaced_export['subtitles'], 'after': revised['subtitles']})
                stage = 'preservation'
                unchanged = all(evidence.file_hash(root / item['path']) == item['sha256']
                                for item in report['artifacts']['source_videos'])
                _check(report, 'source_assets_unchanged', unchanged,
                       {'checked_source_assets': len(report['artifacts']['source_videos'])})
                for name in ('baseline', 'shot_replacement', 'subtitle_revision'):
                    item = report['artifacts'][name]['video']
                    _check(report, name + '_preserved',
                           evidence.file_hash(root / item['path']) == item['sha256'],
                           {'path': item['path'], 'sha256': item['sha256']})
                _check(report, 'no_external_http',
                       report['metrics']['blocked_http_attempts'] == 0,
                       {'blocked_attempts': report['metrics']['blocked_http_attempts']})
            finally:
                report['metrics']['video_generation_calls'] = generated.call_count
    except Exception as exc:
        report['checks'].append({'id': 'execution', 'status': 'failed',
                                 'evidence': {'stage': stage, 'error_type': type(exc).__name__}})
        report['metrics']['elapsed_sec'] = round(time.monotonic() - started, 3)
        try:
            evidence.write_bundle_report(root, report)
        except Exception as report_exc:
            # Never let evidence-writing failure replace the failure under investigation.
            print(f'Failure evidence could not be written ({type(report_exc).__name__}); '
                  f'created directory: {root}', file=sys.stderr)
        raise
    report['metrics']['elapsed_sec'] = round(time.monotonic() - started, 3)
    return evidence.write_bundle_report(root, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_baseline(args.output_root)
    except Exception as exc:
        print(f'Acceptance failed ({type(exc).__name__}); inspect the output directory.', file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
