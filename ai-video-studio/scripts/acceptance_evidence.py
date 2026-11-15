"""Local acceptance evidence, not a production workflow or provider bill."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path


NOT_VERIFIED = (
    'real_provider', 'real_tts', 'one_click_workflow',
    'worker_restart_recovery', 'provider_billing',
)


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inspect_video(path: Path) -> dict:
    """Probe metadata; readable does not certify every frame or visual quality."""
    result = dict.fromkeys(('duration_sec', 'width', 'height', 'fps', 'has_audio',
                            'sha256', 'error'))
    result['readable'] = False
    try:
        result['sha256'] = file_hash(path)
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        data = json.loads(proc.stdout)
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
        duration = float(data['format']['duration'])
        fps = float(Fraction(video['avg_frame_rate']))
        if not math.isfinite(duration) or duration <= 0 or fps <= 0:
            raise ValueError('Invalid video duration or frame rate')
        result.update(
            readable=True, duration_sec=duration, width=int(video['width']),
            height=int(video['height']), fps=fps,
            has_audio=any(s.get('codec_type') == 'audio' for s in data['streams']),
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError,
            StopIteration, ZeroDivisionError) as exc:
        # Do not copy arbitrary tool output or absolute filenames into the report.
        result['error'] = f'{type(exc).__name__}: video could not be probed'
    return result


def _fields(value, required, label):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise ValueError(f'Missing {label} fields')


def _relative_paths(value, root):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'path':
                if not isinstance(child, str) or not child:
                    raise ValueError('Artifact path must be a relative string')
                path = Path(child)
                if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
                    raise ValueError('Artifact path must stay within bundle')
            else:
                _relative_paths(child, root)
    elif isinstance(value, list):
        for child in value:
            _relative_paths(child, root)


def write_bundle_report(root: Path, evidence: dict) -> Path:
    """Validate the acceptance schema and refuse to overwrite any report."""
    _fields(evidence, (
        'schema_version', 'mode', 'source', 'environment', 'project_id', 'artifacts',
        'checks', 'costs', 'manual_steps', 'not_verified', 'metrics',
    ), 'report')
    if evidence['schema_version'] != 1 or evidence['mode'] != 'mock_baseline':
        raise ValueError('Unsupported report schema or mode')
    _fields(evidence['source'], ('commit', 'dirty'), 'source')
    _fields(evidence['environment'], ('python', 'ffmpeg', 'ffprobe'), 'environment')
    for section in ('source', 'environment'):
        if any(v is None for v in evidence[section].values()) and not evidence[section].get('errors'):
            raise ValueError(f'Unknown {section} values require errors')
    _fields(evidence['metrics'], (
        'elapsed_sec', 'video_generation_calls', 'required_clicks', 'measurement_scope',
    ), 'metrics')
    if (evidence['metrics']['required_clicks'] is not None
            or evidence['metrics']['measurement_scope'] != 'legacy_script'):
        raise ValueError('Legacy script cannot measure Guided clicks')
    if not isinstance(evidence['checks'], list) or not evidence['checks']:
        raise ValueError('Checks must be a nonempty list')
    for check in evidence['checks']:
        _fields(check, ('id', 'status', 'evidence'), 'check')
        if check['status'] not in ('passed', 'failed', 'not_verified') or not check['evidence']:
            raise ValueError('Check needs an explicit status and evidence')
    expected_costs = {'external_api_calls': 0, 'estimated_usd': 0,
                      'actual_usd': None, 'billing_status': 'not_applicable'}
    if evidence['costs'] != expected_costs:
        raise ValueError('Mock evidence must not claim real billing')
    if not isinstance(evidence['not_verified'], list) or not set(NOT_VERIFIED) <= set(evidence['not_verified']):
        raise ValueError('Missing required verification limitations')
    if not isinstance(evidence['manual_steps'], list) or not isinstance(evidence['artifacts'], dict):
        raise ValueError('Invalid manual steps or artifacts')
    _relative_paths(evidence['artifacts'], root.resolve())
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    target = root / 'report.json'
    with target.open('x', encoding='utf-8') as stream:
        stream.write(serialized)
    return target
