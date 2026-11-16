"""Per-shot TTS batch tests: idempotency, budget split, partial-failure guard,
and unknown-remote reservation retention.

These tests never call a real TTS API. They either monkeypatch the extracted
``narration._tts_request`` to return a locally synthesized MP3, or drive the
worker directly with a fault injected so the request never leaves the process.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from studio import core, jobs, ledger, narration, worker
from studio import tts_jobs

SCRIPT = {
    'language': 'zh-CN',
    'model': 'gpt-4o-mini-tts',
    'lines': [
        {'seq': 1, 'duration': 10, 'text': '小象走进森林。'},
        {'seq': 2, 'duration': 10, 'text': '它遇见了朋友。'},
    ],
}


def _fake_mp3_bytes(seconds=1.0):
    fd, path = tempfile.mkstemp(suffix='.mp3')
    os.close(fd)
    try:
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
                        '-c:a', 'libmp3lame', path], check=True)
        return Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def tts_env(monkeypatch):
    monkeypatch.setenv('STUDIO_TTS_API_KEY', 'fake-key')


@pytest.fixture(autouse=True)
def handlers():
    from studio.handlers import register_all
    register_all()


@pytest.fixture
def tts_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('A friendly elephant story', 20, 'mock', 10)
    core.plan(pid, 10)
    data = {'project_id': pid, **SCRIPT}
    narration._script_path(pid).write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return pid


@pytest.fixture
def fake_tts(monkeypatch):
    monkeypatch.setattr(narration, '_tts_request',
                        lambda base, key, model, voice, text: _fake_mp3_bytes())


def test_enqueue_batch_is_idempotent(tts_project):
    pid = tts_project
    lines = narration.show(pid)['lines']
    first = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                   budget_usd=2, request_id='batch-1', force=False)
    second = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                    budget_usd=2, request_id='batch-1', force=False)
    assert first['run_id'] == second['run_id']
    assert first['job_ids'] == second['job_ids']
    assert first['request_id'] == 'batch-1'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE kind=?',
                          ('tts_segment',)).fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


def test_enqueue_batch_derives_stable_request_id(tts_project):
    pid = tts_project
    lines = narration.show(pid)['lines']
    first = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                   budget_usd=2, request_id=None, force=False)
    second = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                    budget_usd=2, request_id=None, force=False)
    assert first['request_id'] == second['request_id']
    assert len(first['request_id']) == 32
    assert first['job_ids'] == second['job_ids']


def test_enqueue_batch_allocates_budget_exactly(tts_project):
    pid = tts_project
    lines = narration.show(pid)['lines']
    queued = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                    budget_usd=2, request_id='batch-budget',
                                    force=False)
    total = ledger.to_micro(2)
    with core.connect() as db:
        amounts = [json.loads(db.execute(
            'SELECT payload_json FROM jobs WHERE id=?',
            (job_id,)).fetchone()[0])['budget_microusd']
            for job_id in queued['job_ids']]
    assert sum(amounts) == total
    assert amounts[-1] == total - amounts[0] * (len(lines) - 1)


def test_synthesize_is_idempotent(tts_project, fake_tts):
    pid = tts_project
    first = narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                                 request_id='batch-live', force=True)
    second = narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                                  request_id='batch-live', force=True)
    assert first['run_id'] == second['run_id']
    assert first['job_ids'] == second['job_ids']
    assert (core.HOME / pid / 'narration' / 'narration.wav').is_file()


def test_synthesize_failure_keeps_previous_narration(tts_project, fake_tts, monkeypatch):
    pid = tts_project
    narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                         request_id='batch-good', force=True)
    root = core.HOME / pid / 'narration'
    before = (root / 'narration.wav').read_bytes()
    monkeypatch.setattr(narration, '_tts_request',
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    with pytest.raises(RuntimeError, match='partial'):
        narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                             request_id='batch-bad', force=True)
    assert (root / 'narration.wav').read_bytes() == before


def test_tts_timeout_blocks_unknown_and_retains_reservation(tts_project, monkeypatch):
    pid = tts_project
    lines = narration.show(pid)['lines']
    queued = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                    budget_usd=2, request_id='batch-timeout',
                                    force=False)
    monkeypatch.setattr(narration, '_tts_request',
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    worker.run_once()
    job_id = queued['job_ids'][0]
    saved = jobs.get(job_id)
    assert saved['status'] == 'BLOCKED'
    assert saved['block_reason'] == 'unknown_remote'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (job_id,)).fetchone()[0] == 'reserved'


def test_tts_missing_key_fails_final_and_releases(tts_project, monkeypatch):
    pid = tts_project
    monkeypatch.setattr(narration, 'load_dotenv', lambda: None)
    monkeypatch.delenv('STUDIO_TTS_API_KEY', raising=False)
    monkeypatch.delenv('STUDIO_LLM_API_KEY', raising=False)
    lines = narration.show(pid)['lines']
    queued = tts_jobs.enqueue_batch(pid, lines, voice='alloy', model='m',
                                    budget_usd=2, request_id='batch-nokey',
                                    force=False)
    worker.run_once()
    job_id = queued['job_ids'][0]
    saved = jobs.get(job_id)
    assert saved['status'] == 'FAILED'
    with core.connect() as db:
        row = db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                         (job_id,)).fetchone()
        assert row is not None and row[0] == 'released'
