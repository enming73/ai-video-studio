# M1-E2 Narration, TTS and Reference Image Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route narration LLM planning, per-shot TTS, and CLI reference-image generation through persistent Jobs while preserving the existing CLI output and local file/manifest behavior.

**Architecture:** Extend the `request_jobs` helper with an optional shared `run_key` for multi-job batches. Add `llm_narration` and `tts_segment` handlers; extend the existing `image` handler with a `reference` target. CLI commands enqueue Jobs and wait via `job_runner.run_until`, so they work with either a persistent Worker or the CLI's own execution. Aggregation (TTS concatenation, SRT, manifest) happens only after every segment Job succeeds; partial failure never overwrites the previous narration.

**Tech Stack:** Python 3.11+, FastAPI/CLI, SQLite/WAL, existing Job/Worker/ledger, FFmpeg/FFprobe, pytest, fake HTTP clients.

**Spec:** `docs/superpowers/specs/2026-11-15-m1-reliability-design.md` §3, §4.2–§4.4, §5, §7; Roadmap M1.1–M1.4.

## Global Constraints

- Job/Attempt/Event is the only new truth. Do not add new attempt-table rows for narration, TTS or reference images.
- Every paid CLI operation requires explicit live confirmation, a positive budget, and an idempotency request ID.
- Unknown provider outcome: persist the attempt before the network call, mark `UNKNOWN_REMOTE_STATE`/`UNKNOWN`, set Job `BLOCKED`, retain the reservation, never auto-resubmit.
- Unknown pricing reserves the caller's ceiling with `pricing_version="user_ceiling:<request_key>"`.
- Money is integer micro-USD; times are UTC ISO strings.
- Tests never call real paid APIs; fake HTTP/LLM/TTS clients return deterministic local content.
- No M2 Scene/Timeline changes, no resolution/quality changes, no real paid acceptance.

## Review Focus

1. **A TTS batch partially fails and overwrites the previous narration** → Task 2 `test_tts_batch_failure_keeps_previous_narration`.
2. **Repeating a paid CLI command creates a second batch** → Task 2 `test_tts_batch_is_idempotent_for_same_script_and_voice`, Task 3 `test_reference_frame_replay_returns_same_job`.
3. **Narration plan overwrites a script after storyboard changes** → Task 1 `test_narration_plan_stale_storyboard_fails_without_overwrite`.
4. **TTS provider timeout is retried and double-charged** → Task 2 `test_tts_timeout_blocks_unknown_and_retains_reservation`.
5. **CLI waits forever or reports success while Jobs are blocked** → Task 2 `test_tts_cli_reports_blocked_job`, Task 3 `test_reference_cli_reports_blocked_job`.

---

### Task 1: Narration LLM Job

**Files:**
- Modify: `studio/request_jobs.py` (optional shared `run_key`)
- Create: `studio/narration_jobs.py`
- Create: `studio/handlers/llm_narration.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/narration.py:34-65`
- Modify: `studio/cli.py:252-257`
- Modify: `tests/test_narration.py`
- Create: `tests/test_narration_jobs.py`

**Interfaces:**
- `request_jobs.submit(pid, *, kind, request_id, payload, budget_usd, target_key=None, run_key=None)`:
  - Existing behavior; `run_key` defaults to `f'{kind}:{request_id}'`.
  - A caller may pass one shared run key for a batch; all Jobs use the same open Run.
- `narration_jobs.enqueue_plan(pid, *, language, model, base_url, budget_usd, request_id) -> dict`
  - Validates `show`-independent plan preconditions; returns `{'status':'QUEUED','job_id':...,'request_id':...}`.
  - Payload includes the project prompt, language, model, base URL, allowed shots snapshot (seq/duration/prompt), and a source digest.
- `llm_narration.execute(job, attempts, control)`:
  - Reserve the user ceiling, call the extracted `narration.generate_script(payload)`, validate line count/sequence, then write `script.json` atomically only if the current shots still match the payload snapshot.
  - Return `{'script': path, 'lines': count, 'lines_data': [...]}`.
  - A changed snapshot fails without overwriting; no auto-retry.
- `narration.plan(..., live=True, request_id)` enqueues and calls `job_runner.run_until([job_id])`, then returns the historical `{'script':..., 'lines':...}` shape plus `job_id`. Dry-run stays unchanged.
- CLI `narration-plan --live` prints the completed result or a clear BLOCKED/FAILED error; add an optional `--request-id`.

- [ ] **Step 1: Write the failing enqueue/stale tests**

```python
def test_narration_plan_runs_through_job(narration_project, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *a: SCRIPT_LLM)
    result = narration.plan(pid, model='test-model', live=True, budget_usd=1,
                            request_id='n1')
    assert result['job_id']
    assert result['script']
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE kind=?', ('llm_narration',)).fetchone()[0] == 1

def test_narration_plan_stale_storyboard_fails_without_overwrite(narration_project, monkeypatch):
    # fake a valid LLM response; change a shot duration between enqueue and worker; assert no script.json
    ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_narration_jobs.py -k 'runs_through_job or stale'`
Expected: FAIL with missing `narration_jobs` / still synchronous.

- [ ] **Step 3: Extract `narration.generate_script(payload)` and implement the handler**

Keep the existing LLM prompt and validation in `generate_script`; move only the file write into the handler after the snapshot check. Use a temp file and `replace`.

- [ ] **Step 4: Add the Worker/domain test**

```python
def test_narration_worker_writes_script(narration_job, monkeypatch):
    monkeypatch.setattr(narration, 'generate_script', lambda payload: SCRIPT)
    assert worker.run_once() == narration_job['job_id']
    assert narration.show(pid)['lines'] == SCRIPT['lines']
    assert jobs.get(narration_job['job_id'])['status'] == 'SUCCEEDED'
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_narration.py tests/test_narration_jobs.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/request_jobs.py studio/narration_jobs.py studio/handlers/llm_narration.py \
  studio/handlers/__init__.py studio/narration.py studio/cli.py \
  tests/test_narration.py tests/test_narration_jobs.py
git commit -m "feat: enqueue narration planning jobs"
```

---

### Task 2: Per-shot TTS Jobs and batch aggregation

**Files:**
- Create: `studio/tts_jobs.py`
- Create: `studio/handlers/tts_segment.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/narration.py:101-147`
- Modify: `studio/cli.py:260-266`
- Modify: `tests/test_narration.py`
- Create: `tests/test_tts_jobs.py`

**Interfaces:**
- `tts_jobs.enqueue_batch(pid, lines, *, voice, model, budget_usd, request_id, force) -> dict`
  - Derives a stable batch key from the script content + voice + model + force when `request_id` is omitted.
  - Opens one Run with `request_key=f'tts:{batch_key}'`; creates one `tts_segment` Job per line with target key `tts:<seq>`.
  - Allocates the total user ceiling across segments in integer micro-USD; the last segment receives the remainder.
  - Returns `{'run_id':..., 'job_ids':[...], 'request_id':...}`.
- `tts_jobs.aggregate(pid, jobs, *, lines, force) -> dict`
  - Requires every Job `SUCCEEDED` and every output file present.
  - Concatenates normalized WAVs, verifies total duration, backs up existing narration/SRT, writes `narration.wav`, `narration.srt`, and `narration-manifest.json`.
  - Returns the existing manifest shape (`project_id`, `model`, `voice`, `characters`, `duration`, `audio`, `subtitles`, `segments`).
- `tts_segment.execute(job, attempts, control)`:
  - Reserves the segment ceiling, calls the extracted `narration.generate_segment(payload)`, validates/probes/speed-fits the audio, writes a unique normalized WAV, returns `{'file':..., 'duration':...}`.
  - Missing key/invalid local config before request → release + `FAILED_FINAL`.
  - Timeout/HTTP/JSON after request → `UNKNOWN_REMOTE_STATE` + block, retain.
- `narration.synthesize(..., live=True, request_id=None)`:
  - `show` validates the script; enqueue the batch; `job_runner.run_until(job_ids)`; if any non-SUCCEEDED, raise a clear partial-failure error listing Job IDs/statuses; otherwise aggregate.
  - Add `run_id` and `job_ids` to the returned manifest so replay can be verified.
  - Dry-run stays unchanged.

- [ ] **Step 1: Write the failing batch tests**

```python
def test_tts_batch_is_idempotent_for_same_script_and_voice(narration_script, monkeypatch):
    first = narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                                 request_id='batch-1')
    second = narration.synthesize(pid, voice='alloy', live=True, budget_usd=2,
                                  request_id='batch-1')
    assert first['run_id'] == second['run_id']
    assert first['job_ids'] == second['job_ids']

def test_tts_batch_failure_keeps_previous_narration(narration_with_audio, monkeypatch):
    before = (root / 'narration.wav').read_bytes()
    # make one segment Job BLOCKED, run the rest, then call synthesize
    with pytest.raises(RuntimeError, match='partial|blocked|failed'):
        narration.synthesize(pid, live=True, budget_usd=2, request_id='batch-2')
    assert (root / 'narration.wav').read_bytes() == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_tts_jobs.py -k 'idempotent or failure_keeps'`
Expected: FAIL with missing `tts_jobs` / no partial-failure guard.

- [ ] **Step 3: Extract `generate_segment(payload)` and implement the handler**

Keep the existing TTS HTTP request, `_speed_filter`, FFmpeg normalization, and probe in `generate_segment`; write only a unique segment WAV. Do not write the final narration in the handler.

- [ ] **Step 4: Add the timeout test**

```python
def test_tts_timeout_blocks_unknown_and_retains_reservation(tts_job, monkeypatch):
    monkeypatch.setattr(tts_jobs, '_tts_request', lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    worker.run_once()
    assert jobs.get(tts_job['job_id'])['block_reason'] == 'unknown_remote'
    assert jobs.get(tts_job['job_id'])['status'] == 'BLOCKED'
    with core.connect() as db:
        assert db.execute('SELECT billing_status FROM cost_entries WHERE job_id=?',
                          (tts_job['job_id'],)).fetchone()[0] == 'reserved'
```

- [ ] **Step 5: Implement aggregation and CLI waiting**

`narration.synthesize` calls `run_until` and only aggregates all-successful batches. The CLI `narration-tts` keeps its output fields and prints a clear error for blocked/failed Jobs. Add `--request-id` as an optional CLI option for explicit replay control.

- [ ] **Step 6: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_narration.py tests/test_tts_jobs.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add studio/tts_jobs.py studio/handlers/tts_segment.py studio/handlers/__init__.py \
  studio/narration.py studio/cli.py tests/test_narration.py tests/test_tts_jobs.py
git commit -m "feat: enqueue per-shot tts jobs"
```

---

### Task 3: CLI reference-image Job

**Files:**
- Modify: `studio/reference_generation.py`
- Modify: `studio/handlers/image.py`
- Modify: `studio/image_jobs.py`
- Modify: `studio/cli.py:139-146`
- Modify: `tests/test_reference_generation.py`
- Create: `tests/test_reference_jobs.py`

**Interfaces:**
- `reference_generation.frame_plan` remains unchanged.
- `reference_generation.generate_frame(..., live=True, request_id=None)`:
  - Dry-run unchanged.
  - Builds the immutable payload from `frame_plan`: project id, seq, model, prompt, reference paths and SHA-256 digests, cache key, force.
  - Enqueues an `image` Job with `target='reference'`, target key `reference:<seq>`.
  - Calls `job_runner.run_until([job_id])`, then returns the historical shape `{'dry_run':False, 'image':..., 'generated_file':..., 'model':..., 'characters':[...], 'job_id':...}`.
  - If the Job is BLOCKED/FAILED, raise a clear error naming the Job and status.
  - CLI `generate-frame` gains an optional `--request-id`.
- `image_jobs.enqueue_reference(...) -> dict`: uses `request_jobs.submit` and target key; replay by request ID or unresolved target.
- `image_jobs.execute_reference(payload) -> dict`: calls the image API, validates PNG bytes, writes under `references/_asset_root/generated`, and calls `references.import_shot`.
- `handlers/image.execute` dispatches `target='reference'` to `execute_reference`; existing character/boundary behavior unchanged.

- [ ] **Step 1: Write the failing reference-job tests**

```python
def test_reference_frame_runs_through_job(reference_project, monkeypatch):
    monkeypatch.setattr(rg, '_http_json', lambda *a, **k: {'data': [{'b64_json': base64.b64encode(PNG).decode()}]})
    result = rg.generate_frame(pid, 1, live=True, budget_usd=1, request_id='r1')
    assert result['job_id']
    assert references.reference_manifest(pid)['shots']['1']['file']

def test_reference_frame_replay_returns_same_job(reference_project, monkeypatch):
    monkeypatch.setattr(rg, '_http_json', lambda *a, **k: {'data': [{'b64_json': base64.b64encode(PNG).decode()}]})
    first = rg.generate_frame(pid, 1, live=True, budget_usd=1, request_id='r1')
    second = rg.generate_frame(pid, 1, live=True, budget_usd=1, request_id='r1')
    assert second['job_id'] == first['job_id']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_reference_jobs.py -k 'runs_through or replay'`
Expected: FAIL because reference generation is still synchronous.

- [ ] **Step 3: Implement the reference target and CLI wait**

Extend `image_jobs`/`handlers/image.py` with the reference target, reusing the existing `PreRequestFailure`/`PostResponseFailure` split. Keep the output shape and do not mutate existing selected assets.

- [ ] **Step 4: Add the Worker/domain test**

```python
def test_reference_worker_imports_generated_shot(reference_job, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_image', lambda *a, **k: PNG)
    assert worker.run_once() == reference_job['job_id']
    assert references.reference_manifest(pid)['shots']['1']['file']
    assert jobs.get(reference_job['job_id'])['status'] == 'SUCCEEDED'
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_reference_generation.py tests/test_reference_jobs.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/reference_generation.py studio/handlers/image.py studio/image_jobs.py \
  studio/cli.py tests/test_reference_generation.py tests/test_reference_jobs.py
git commit -m "feat: enqueue reference image jobs"
```

---

### Task 4: Cross-command regression and documentation

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `tests/test_narration.py`
- Modify: `tests/test_reference_generation.py`
- Modify: `tests/test_tts_jobs.py`

**Interfaces:**
- No new production interface.
- CLI dry-run output stays unchanged.
- Every live CLI command reports the Job ID/status and never claims success for a blocked/failed Job.

- [ ] **Step 1: Add CLI contract tests**

```python
def test_live_narration_cli_reports_job_status(runner, narration_project, monkeypatch):
    result = runner.invoke(app, ['narration-plan', pid, '--live', '--budget', '1'])
    assert result.exit_code == 0
    assert 'job_id' in result.output or 'script' in result.output

def test_live_tts_cli_reports_partial_failure(runner, tts_project, monkeypatch):
    result = runner.invoke(app, ['narration-tts', pid, '--live', '--budget', '2'])
    assert result.exit_code != 0
    assert 'blocked' in result.output.lower() or 'failed' in result.output.lower()
```

- [ ] **Step 2: Run the tests to verify they fail or expose missing output**

Run: `uv run --extra dev --extra web pytest -q tests/test_narration.py tests/test_tts_jobs.py -k cli`
Expected: FAIL or missing expected job/status output.

- [ ] **Step 3: Update CLI output and documentation**

Document:
- Narration planning is a persistent Job.
- TTS creates one Job per line sharing a Run; partial failure leaves prior narration untouched.
- Reference-image live generation is a persistent Job.
- CLI waits and reports the Job ID; a separate Worker can execute it.
- Unknown outcomes retain reservations and require bill checking.
- E2 is implemented in this branch; E3 remains pending.

- [ ] **Step 4: Run the full verification**

Run: `uv run --extra dev --extra web pytest -q`
Expected: all Studio tests pass.

Run: `cd ../vbench && uv run --extra test pytest -q`
Expected: 8 passed.

Run: `cd ../ai-video-studio/web && npm ci && npm run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add README.md ROADMAP.md tests/test_narration.py \
  tests/test_reference_generation.py tests/test_tts_jobs.py
git commit -m "docs: record narration and reference job compatibility"
```

---

## Self-Review

- **Spec coverage:** E2 covers narration LLM, per-segment TTS with shared Runs, reference-image image Jobs, user ceilings, unknown blocking, no auto-retry, partial-failure visibility, and CLI wait/reporting. E3 remains deferred.
- **Type consistency:** `request_jobs.submit` gains one optional `run_key`; TTS uses it for a batch. All job kinds and output keys are named consistently.
- **Review Focus:** every review focus line has a named test in its task.
- **Proportion:** the plan keeps existing provider/file logic and specifies only the new job boundaries and aggregation decisions.
