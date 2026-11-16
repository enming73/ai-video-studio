"""Enqueue per-shot TTS jobs and aggregate their normalized WAVs into one narration.

One ``narration-tts`` command opens a single Run and one ``tts_segment`` Job per
script line. A stable batch key (an explicit ``request_id`` or a content hash)
makes a repeated identical live command idempotent: the same Run and Jobs are
reused, and no second paid request is ever opened for the same work.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from . import core, ledger, narration, request_jobs


def _batch_key(lines, voice, model, force):
    canonical = json.dumps({'lines': lines, 'voice': voice, 'model': model,
                            'force': force},
                           sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


def enqueue_batch(pid, lines, *, voice, model, budget_usd, request_id, force):
    """Open one shared Run and enqueue one ``tts_segment`` Job per line."""
    core.require_paid_budget(budget_usd)
    if not isinstance(model, str) or not model.strip():
        raise ValueError('TTS model is required')
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError('Voice is required')
    if not lines:
        raise ValueError('No narration lines to synthesize')
    if request_id is None:
        request_id = _batch_key(lines, voice, model, force)
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Request id required')
    # A safe, deterministic directory name: the user request id never becomes a
    # path component, so a hostile or over-long id cannot escape the narration
    # directory or overflow the per-segment request key.
    path_key = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:32]
    total_micro = ledger.to_micro(budget_usd)
    n = len(lines)
    if total_micro < n:
        raise ValueError('Budget is too small to allocate one micro-USD per segment')
    each = total_micro // n
    allocations = [each] * (n - 1) + [total_micro - each * (n - 1)]
    assert sum(allocations) == total_micro
    base_url = os.getenv('STUDIO_TTS_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    segment_dir = narration._root(pid) / 'segments' / path_key
    run_key = f'tts:{request_id}'
    created = []
    for line, amount in zip(lines, allocations):
        payload = {
            'project_id': pid,
            'seq': line['seq'],
            'text': line['text'],
            'duration': line['duration'],
            'voice': voice,
            'model': model,
            'base_url': base_url,
            'budget_microusd': amount,
            'segment_dir': str(segment_dir),
        }
        job = request_jobs.submit(
            pid, kind='tts_segment', request_id=f'{path_key}:{line["seq"]}',
            payload=payload, budget_usd=budget_usd,
            target_key=f'tts:{line["seq"]}', run_key=run_key)
        created.append(job)
    return {'run_id': created[0]['run_id'], 'job_ids': [j['id'] for j in created],
            'request_id': request_id}


def aggregate(pid, jobs, *, force) -> dict:
    """Concat every SUCCEEDED segment in seq order and write the final narration.

    The subtitles, total duration and character count are derived from each
    Job's immutable ``payload_json`` (not the caller's current script lines), so
    the WAVs, SRT and manifest always describe the same synthesized content —
    including on an explicit replay after a script edit.
    """
    from . import sound
    if not jobs:
        raise ValueError('No TTS segment jobs to aggregate')
    ordered = sorted(jobs, key=lambda j: json.loads(j['payload_json'])['seq'])
    payloads = [json.loads(j['payload_json']) for j in ordered]
    lines = [{'seq': p['seq'], 'text': p['text'], 'duration': p['duration']}
             for p in payloads]
    voice = payloads[0]['voice']; model = payloads[0]['model']
    root = narration._root(pid)
    segments = []
    for job in ordered:
        if job['status'] != 'SUCCEEDED':
            raise RuntimeError(f'tts segment job {job["id"]} is {job["status"]}')
        output = json.loads(job['output_json'] or '{}')
        path = output.get('file')
        if not path or not Path(path).is_file():
            raise RuntimeError(f'tts segment job {job["id"]} is missing its WAV output')
        segments.append(Path(path))
    run_dir = root / ('agg-' + uuid.uuid4().hex[:10]); run_dir.mkdir()
    concat = run_dir / 'concat.txt'
    concat.write_text(''.join(
        "file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in segments),
        encoding='utf-8')
    temp = run_dir / 'combined.wav'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-n', '-f', 'concat',
                    '-safe', '0', '-i', str(concat), '-c:a', 'pcm_s16le', str(temp)],
                   check=True)
    duration = sum(float(x['duration']) for x in lines)
    if abs(sound._probe(temp) - duration) > 0.15:
        raise RuntimeError('Combined narration duration mismatch')
    output = root / 'narration.wav'; captions = root / 'narration.srt'
    if output.exists():
        output.rename(root / ('narration-backup-' + uuid.uuid4().hex[:8] + '.wav'))
    if captions.exists():
        captions.rename(root / ('narration-backup-' + uuid.uuid4().hex[:8] + '.srt'))
    temp.replace(output)
    narration._srt(lines, captions)
    total = sum(len(x['text']) for x in lines)
    manifest = {'project_id': pid, 'model': model, 'voice': voice, 'characters': total,
                'duration': duration, 'audio': str(output), 'subtitles': str(captions),
                'segments': len(segments)}
    (root / 'narration-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
