# M1-E1 Guided Paid Entry Points Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the remaining Guided paid entry points — storyboard LLM, character image, boundary-frame image, and boundary-prompt LLM — through persistent Jobs while keeping the existing Guided API fields, historical rows, and React workbench usable.

**Architecture:** Add a small shared request-job helper for run/job creation, active-target reuse, request aliases, and latest-attempt lookup. Each Guided paid function validates its existing preconditions, enqueues one Job with an immutable payload, and returns a compatibility projection with `QUEUED` or `SUBMITTING`. New handlers execute the existing provider/domain logic in the Worker; old attempt tables remain historical only. Projections merge historical rows with Job attempts and never dual-write.

**Tech Stack:** Python 3.11+, FastAPI, SQLite/WAL, existing Job/Worker/ledger services, React/Vite, pytest, fake provider clients.

**Spec:** `docs/superpowers/specs/2026-11-15-m1-reliability-design.md` §3, §4.2–§4.4, §5, §7, §8; Roadmap M1.1–M1.4.

## Delivery Slices

M1-E is split because its subsystems have independent APIs and failure modes:

- **E1 (this plan):** Guided storyboard LLM, character image, boundary-frame image, boundary-prompt LLM.
- **E2 (follow-up plan):** narration LLM, per-shot TTS jobs with aggregation, and CLI reference-image generation.
- **E3 (follow-up plan):** Director and continuity vision reviews as per-shot/per-pair Jobs with report aggregation.

Do not implement E2/E3 here.

## Global Constraints

- Job/Attempt/Event is the only new truth. Do not insert new rows into `guided_planning_attempts`, `guided_image_attempts`, or `guided_boundary_prompt_attempts`; old rows remain readable.
- Every paid entry point requires explicit `confirm_paid`/`confirm_checked`, a positive budget, and an idempotency request ID.
- Unknown provider outcome: persist the attempt before the network call, mark it `UNKNOWN_REMOTE_STATE`/`UNKNOWN`, set Job `BLOCKED`, retain the reservation, and never auto-resubmit.
- When provider pricing is unknown, reserve the caller's ceiling with `pricing_version="user_ceiling:<request_key>"`; never reserve zero to pretend coverage.
- All monetary values are integer micro-USD; all times are UTC ISO strings.
- Preserve existing Guided URLs and response field names. If a state cannot map, return an explicit compatibility error rather than silently dropping a field.
- React changes are limited to polling, confirmation fields, warning conditions, and refresh after completion.
- Tests never call real paid APIs; provider/LLM clients are faked.
- No M2 Scene/Timeline work, no resolution/quality changes, no real paid acceptance in this plan.

## Review Focus

1. **Double click creates two paid jobs** → Task 1 `test_request_job_reuses_unresolved_target`, Task 2 `test_character_image_double_click_reuses_job`, Task 3 `test_prompt_double_click_reuses_job`.
2. **Stale revision gets overwritten after a slow provider** → Task 1 `test_storyboard_job_rejects_stale_revision_without_overwrite`, Task 2 `test_boundary_image_job_marks_result_stale_when_snapshot_changed`, Task 3 `test_prompt_job_does_not_overwrite_changed_boundary`.
3. **Provider timeout is retried and double-charged** → Task 2 `test_image_timeout_blocks_unknown_without_resubmit`, Task 3 `test_prompt_timeout_blocks_unknown_without_resubmit`.
4. **Historical attempts disappear from the workbench** → Task 1 `test_plan_attempts_merge_legacy_and_jobs`, Task 2 `test_image_attempts_merge_legacy_and_jobs`, Task 3 `test_prompt_attempts_merge_legacy_and_jobs`.
5. **Unknown Job blocks the whole list or shows a false warning** → Task 4 `test_attempt_lists_survive_blocked_jobs` and the UI polling/warning test checklist.

---

### Task 1: Shared request jobs + Guided storyboard LLM

**Files:**
- Create: `studio/request_jobs.py`
- Create: `studio/handlers/llm_storyboard.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/guided_storyboard.py:41-99`
- Modify: `studio/guided_compat.py`
- Modify: `studio/guided_web.py:339-389`
- Modify: `web/src/GuidedProductionWorkspace.jsx`
- Modify: `tests/test_guided_storyboard.py`
- Modify: `tests/test_guided_web.py`
- Create: `tests/test_request_jobs.py`

**Interfaces:**
- `request_jobs.submit(pid: str, *, kind: str, request_id: str, payload: dict, budget_usd: float, target_key: str | None = None) -> dict`
  - Rejects non-positive/non-finite budgets.
  - First resolves idempotency without opening a new run: if the same request ID already exists, return its Job only when `kind` and `target_key` match; otherwise raise `ValueError('Request id belongs to another operation')`.
  - If `target_key` has an unresolved Job (`PENDING`, `RUNNING`, `PAUSED`, `BLOCKED`), returns that Job and records the new request ID as an alias event `request_alias`.
  - Only for a new request/target: opens or reuses `run_id` with key `f'{kind}:{request_id}'` and creates `jobs.create(..., request_key=f'{kind}:{request_id}', target_key=target_key)`.
  - Target keys: storyboard `storyboard`, character `character:<id>`, boundary image `boundary:<id>`, boundary prompt `prompt:<boundary_id>`.
- `request_jobs.find(pid: str, *, kind: str, request_id: str, db=None) -> dict | None`
  - Finds by `request_key` or `request_alias` event; verifies `kind`.
- `request_jobs.latest_attempt(job: dict, *, db=None) -> dict | None`
- `guided_compat.project_plan_job(job: dict, *, db=None) -> dict`
  - Legacy fields exactly: `project_id, request_id, status, model, budget_usd, result_json, error`.
  - PENDING/RUNNING without attempt → `QUEUED`; attempt SUBMITTING → `SUBMITTING`; COMPLETED → `COMPLETED`; `BLOCKED` with `block_reason='unknown_remote'` → `UNKNOWN`; other `BLOCKED` → `FAILED` with the block reason in `error`; FAILED → `FAILED`; CANCELLED/ABANDONED → `ACKNOWLEDGED`.
  - `result_json` is `json.dumps(job.output_json['result'])` when present.
- `guided_compat.plan_attempt_records(pid: str, db=None) -> list[dict]`
  - Historical `guided_planning_attempts` rows plus `kind='llm_storyboard'` Job projections, sorted Job-newest-first, legacy rowid order after.
- `guided_compat.find_plan(pid: str, request_id: str, db=None) -> dict`
  - Legacy row first; otherwise the Job projection; raise `ValueError('Unknown planning attempt')`.
- `guided_storyboard.request_plan(pid: str, model: str, budget_usd: float, *, confirm_paid: bool, shot_length: int | None = None, request_id: str, revision: int) -> dict`
  - Keeps `preview` validation, explicit budget/confirm checks, and revision check.
  - Returns `project_plan_job(job)` with `status="QUEUED"`.
- `guided_web` `/plan/attempts/{request_id}/resolve` accepts `{"confirm_checked": true}`; for a Job request it abandons the latest unknown attempt, cancels the Job, retains the reservation, and returns the projection. Legacy rows keep the existing acknowledgement behavior but also require `confirm_checked=true`.
- `handlers/llm_storyboard.execute(job, attempts, control)`:
  - Reserves `ledger.to_micro(payload['budget_usd'])` with `pricing_version=f'user_ceiling:{job["request_key"]}'`.
  - Calls the extracted pure `guided_storyboard.generate_draft(payload)`.
  - Calls `guided_workflow.save_draft(pid, draft, payload['revision'])`; stale revision raises without writing, and the handler marks the attempt `FAILED_FINAL` before re-raising.
  - Returns `{'result': draft}`.
- `handlers/llm_storyboard` registered as kind `llm_storyboard`.

- [ ] **Step 1: Write the failing shared-helper and projection tests**

```python
@pytest.fixture
def job_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    return core.create('A river', 10, 'minimax', 5)

def test_request_job_reuses_unresolved_target(job_project):
    pid = job_project
    first = request_jobs.submit(pid, kind='image', request_id='a', target_key='character:c1',
                                payload={'x': 1}, budget_usd=1)
    second = request_jobs.submit(pid, kind='image', request_id='b', target_key='character:c1',
                                 payload={'x': 1}, budget_usd=1)
    assert first['id'] == second['id']
    assert request_jobs.find(pid, kind='image', request_id='b')['id'] == first['id']

def test_plan_attempts_merge_legacy_and_jobs(guided_storyboard_job):
    with core.connect() as db:
        db.execute('INSERT INTO guided_planning_attempts VALUES (?,?,?,?,?,?,?)',
                   (pid, 'old', 'UNKNOWN', 'm', 1, None, 'old failure'))
    rows = guided_compat.plan_attempt_records(pid)
    assert [r['request_id'] for r in rows] == ['new', 'old']
    assert set(rows[0]) == {'project_id','request_id','status','model','budget_usd','result_json','error'}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_request_jobs.py tests/test_guided_storyboard.py -k 'request_job or plan_attempts'`
Expected: FAIL with missing `studio.request_jobs` / `project_plan_job`.

- [ ] **Step 3: Implement the shared helper, plan projection, and storyboard enqueue**

Extract the existing LLM prompt construction/validation from `request_plan` into `generate_draft(payload: dict) -> dict`. Keep `preview` unchanged. On enqueue, return the projection; do not call the LLM in the API process.

- [ ] **Step 4: Add the failing Worker/domain test**

```python
def test_storyboard_worker_saves_draft_and_completes(guided_storyboard_job, monkeypatch):
    monkeypatch.setattr(guided_storyboard, 'generate_draft', lambda payload: DRAFT)
    assert worker.run_once() == guided_storyboard_job['id']
    assert guided_workflow.get(pid)['draft'] == DRAFT
    assert guided_compat.find_plan(pid, 'new')['status'] == 'COMPLETED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guided_planning_attempts').fetchone()[0] == 0
```

Add `test_storyboard_job_rejects_stale_revision_without_overwrite` by changing the workflow revision after enqueue; expect Job `FAILED` and no draft write.

- [ ] **Step 5: Run tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_storyboard.py tests/test_guided_web.py -k 'storyboard_worker or stale_revision'`
Expected: FAIL because the handler is not registered or no draft is saved.

- [ ] **Step 6: Register the handler and change the plan endpoint**

Register the handler. Change `guided_web.plan` to call `request_plan(..., request_id=body.request_id, revision=body.revision)` and return the projection. Change the resolve endpoint to require `confirm_checked=true`.

- [ ] **Step 7: Update the React planning flow**

In `GuidedProductionWorkspace.jsx`, poll `/plan/attempts` while an attempt is `QUEUED`/`SUBMITTING` without `error`; show the unresolved warning only for `UNKNOWN` or an explicit error; refresh the workflow after `COMPLETED`; send `confirm_checked:true` to resolve.

- [ ] **Step 8: Run the focused tests to verify they pass**

Run: `uv run --extra dev --extra web pytest -q tests/test_request_jobs.py tests/test_guided_storyboard.py tests/test_guided_web.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add studio/request_jobs.py studio/handlers/llm_storyboard.py studio/handlers/__init__.py \
  studio/guided_storyboard.py studio/guided_compat.py studio/guided_web.py \
  web/src/GuidedProductionWorkspace.jsx tests/test_request_jobs.py \
  tests/test_guided_storyboard.py tests/test_guided_web.py
git commit -m "feat: enqueue guided storyboard planning jobs"
```

---

### Task 2: Guided character and boundary image Jobs

**Files:**
- Create: `studio/handlers/image.py`
- Create: `studio/image_jobs.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/guided_images.py:229-274`
- Modify: `studio/guided_boundaries.py:431-501`
- Modify: `studio/guided_compat.py`
- Modify: `studio/guided_web.py:205-276`
- Modify: `web/src/GuidedCharacters.jsx`
- Modify: `web/src/GuidedBoundaryFrames.jsx`
- Modify: `tests/test_guided_images.py`
- Modify: `tests/test_guided_boundary_images.py`
- Modify: `tests/test_guided_web.py`

**Interfaces:**
- `image_jobs.enqueue_character(pid, character_id, model, budget_usd, *, confirm_paid, request_id, revision) -> dict`
- `image_jobs.enqueue_boundary(pid, boundary_id, model, budget_usd, *, confirm_paid, request_id, revision) -> dict`
  - Both retain existing preview/validation and return `guided_compat.project_image_job(job)` with `status="QUEUED"`.
- `image_jobs.execute_character(payload: dict) -> dict` and `image_jobs.execute_boundary(payload: dict) -> dict`
  - Move the provider request, PNG validation, 16:9 crop, and domain-row writes out of the current request functions.
  - Never write `guided_image_attempts`; write `guided_character_images` / `guided_boundary_versions` only.
- `guided_compat.project_image_job(job: dict, *, db=None) -> dict`
  - Legacy fields exactly: `id, target_id, kind, request_id, model, budget, status, image_id, error`.
  - PENDING/RUNNING without attempt → `QUEUED`; attempt SUBMITTING → `SUBMITTING`; COMPLETED → `COMPLETED`; `BLOCKED` with `block_reason='unknown_remote'` or attempt `UNKNOWN_REMOTE_STATE` → `UNKNOWN`; other `BLOCKED` → `FAILED` with the block reason in `error`; FAILED → `FAILED`; CANCELLED/ABANDONED → `ACKNOWLEDGED`.
- `guided_compat.image_attempt_records(pid, db=None) -> list[dict]`
  - Merge legacy `guided_image_attempts` with `kind='image'` Job projections, dedupe by `id`.
- `guided_compat.find_image(pid, attempt_id, db=None) -> dict`
  - Legacy row first; otherwise the Job projection by Job ID or latest attempt ID.
- `handlers/image.execute(job, attempts, control)`:
  - Reserve the user ceiling before the first network call.
  - On provider timeout/HTTP/JSON error after the request may have been sent, mark attempt `UNKNOWN_REMOTE_STATE`, append `image_unknown`, block the Job as `unknown_remote`.
  - On success, write the image/domain rows and return `{'image_id': ...}`.

- [ ] **Step 1: Write the failing enqueue/idempotency tests**

```python
def test_character_image_double_click_reuses_job(project, monkeypatch):
    first = guided_images.generate_character(pid, character_id, MODEL, 1, confirm_paid=True,
                                             request_id='a', revision=revision)
    second = guided_images.generate_character(pid, character_id, MODEL, 1, confirm_paid=True,
                                              request_id='b', revision=revision)
    assert first['id'] == second['id']
    assert first['status'] == 'QUEUED'
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM guided_image_attempts').fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_images.py tests/test_guided_boundary_images.py -k 'double_click or merge'`
Expected: FAIL with missing `project_image_job` / still writing legacy rows.

- [ ] **Step 3: Implement the image handler and both enqueue functions**

Move provider/domain code into `image_jobs`; keep `preview_character`, `preview`, references, crop, and revision validation in the API path. Handler uses `payload['target']` to dispatch character or boundary.

- [ ] **Step 4: Add the failing unknown-provider test**

```python
def test_image_timeout_blocks_unknown_without_resubmit(job, monkeypatch):
    monkeypatch.setattr(image_jobs, '_request_image', lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout')))
    worker.run_once()
    assert jobs.get(job['id'])['block_reason'] == 'unknown_remote'
    assert guided_compat.find_image(pid, job['id'])['status'] == 'UNKNOWN'
    with core.connect() as db:
        assert db.execute("SELECT billing_status FROM cost_entries WHERE job_id=?", (job['id'],)).fetchone()[0] == 'reserved'
    with pytest.raises(ValueError, match='unresolved'):
        image_jobs.enqueue_character(pid, character_id, MODEL, 1, confirm_paid=True,
                                     request_id='retry', revision=revision)
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_images.py -k timeout_blocks_unknown`
Expected: FAIL because the unknown state or reservation is not persisted.

- [ ] **Step 6: Implement unknown handling and projections**

Persist `UNKNOWN_REMOTE_STATE` before blocking, merge legacy rows with Job projections, and update `/image-attempts` plus both image generation endpoints to use the projection helpers.

- [ ] **Step 7: Update the React image flows**

Poll `/image-attempts` for `QUEUED`/`SUBMITTING` without error, refresh images after `COMPLETED`, show the warning only for `UNKNOWN`/error, and send `confirm_checked:true` to resolve.

- [ ] **Step 8: Run the focused tests to verify they pass**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_images.py tests/test_guided_boundary_images.py tests/test_guided_web.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add studio/handlers/image.py studio/image_jobs.py studio/handlers/__init__.py \
  studio/guided_images.py studio/guided_boundaries.py studio/guided_compat.py \
  studio/guided_web.py web/src/GuidedCharacters.jsx web/src/GuidedBoundaryFrames.jsx \
  tests/test_guided_images.py tests/test_guided_boundary_images.py tests/test_guided_web.py
git commit -m "feat: enqueue guided image jobs"
```

---

### Task 3: Guided boundary-prompt LLM Job

**Files:**
- Create: `studio/handlers/llm_prompt.py`
- Create: `studio/prompt_jobs.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/guided_boundaries.py:266-359`
- Modify: `studio/guided_compat.py`
- Modify: `studio/guided_web.py:295-326`
- Modify: `web/src/GuidedBoundaryFrames.jsx`
- Modify: `tests/test_guided_boundary_prompts.py`
- Modify: `tests/test_guided_web.py`

**Interfaces:**
- `prompt_jobs.enqueue(pid, boundary_id, model, budget_usd, *, confirm_paid, request_id, revision) -> dict`
  - Keeps `preview_prompt` and revision checks; returns `guided_compat.project_prompt_job(job)` with `status="QUEUED"`.
- `prompt_jobs.execute(payload: dict) -> dict`
  - Calls `core._llm_json(_LLM_SYSTEM, ...)`; validates prompt length; writes `guided_boundary_prompts` and increments the workflow revision only if the boundary/source snapshot is still current.
- `guided_compat.project_prompt_job(job: dict, *, db=None) -> dict`
  - Legacy fields exactly: `id, project_id, boundary_id, request_id, status, model, budget_usd, source_sha256, result_prompt, error, created`.
  - PENDING/RUNNING → `QUEUED`; SUBMITTING → `SUBMITTING`; COMPLETED → `COMPLETED`; `BLOCKED` with `block_reason='unknown_remote'` or attempt `UNKNOWN_REMOTE_STATE` → `UNKNOWN`; other `BLOCKED` → `FAILED` with the block reason in `error`; FAILED → `FAILED`; CANCELLED/ABANDONED → `ACKNOWLEDGED`.
- `guided_compat.prompt_attempt_records(pid, db=None) -> list[dict]`
- `guided_compat.find_prompt(pid, attempt_id, db=None) -> dict`
  - Legacy row first; otherwise the Job projection by Job ID or latest attempt ID.
- `handlers/llm_prompt.execute(job, attempts, control)`:
  - Reserve the user ceiling; on timeout/invalid response after a request may have been sent, mark `UNKNOWN_REMOTE_STATE` and block `unknown_remote`.
  - On a valid response with a changed boundary/source snapshot, do not write the prompt; mark Job `FAILED` with `stale_revision` evidence.

- [ ] **Step 1: Write the failing prompt enqueue test**

```python
def test_prompt_double_click_reuses_job(prompt_project, monkeypatch):
    first = guided_boundaries.generate_prompt(pid, boundary_id, 'm', 1,
        confirm_paid=True, request_id='a', revision=revision)
    second = guided_boundaries.generate_prompt(pid, boundary_id, 'm', 1,
        confirm_paid=True, request_id='b', revision=revision)
    assert first['id'] == second['id']
    assert first['status'] == 'QUEUED'
    assert guided_compat.find_prompt(pid, 'b')['id'] == first['id']
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_boundary_prompts.py -k double_click`
Expected: FAIL with missing `project_prompt_job`.

- [ ] **Step 3: Implement `prompt_jobs`, handler registration, and projection**

Move the LLM call and validation into `prompt_jobs.execute`; leave `preview_prompt`/`clear_prompt` unchanged. Update `/prompt-attempts` and resolve to use projections. The resolve action cancels/acknowledges the Job rather than editing a legacy row.

- [ ] **Step 4: Add the failing stale-snapshot test**

```python
def test_prompt_job_does_not_overwrite_changed_boundary(prompt_job, monkeypatch):
    monkeypatch.setattr(core, '_llm_json', lambda *a: {'prompt': 'A new prompt ' * 4})
    with core.connect() as db:
        db.execute("UPDATE guided_boundary_prompts SET result_prompt='edited' WHERE boundary_id=?", (boundary_id,))
    worker.run_once()
    assert jobs.get(prompt_job['id'])['status'] == 'FAILED'
    assert guided_boundaries.prompt_state(pid, boundary_id)['effective_prompt'] == 'edited'
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_boundary_prompts.py -k changed_boundary`
Expected: FAIL because the handler is not registered or the old prompt is overwritten.

- [ ] **Step 6: Implement the prompt handler and resolve mapping**

Register the handler, make the prompt endpoints read projections, and map resolve to abandon/cancel the latest unknown attempt while retaining the reservation.

- [ ] **Step 7: Update the React prompt flow**

Poll `/prompt-attempts` while `QUEUED`/`SUBMITTING` without error; refresh boundary prompt state after `COMPLETED`; show the warning only for `UNKNOWN`/error; send `confirm_checked:true` to resolve.

- [ ] **Step 8: Run the focused tests to verify they pass**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_boundary_prompts.py tests/test_guided_web.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add studio/handlers/llm_prompt.py studio/prompt_jobs.py studio/handlers/__init__.py \
  studio/guided_boundaries.py studio/guided_compat.py studio/guided_web.py \
  web/src/GuidedBoundaryFrames.jsx tests/test_guided_boundary_prompts.py tests/test_guided_web.py
git commit -m "feat: enqueue guided boundary prompt jobs"
```

---

### Task 4: Cross-entry compatibility regression and documentation

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `tests/test_guided_web.py`
- Modify: `tests/test_guided_images.py`
- Modify: `tests/test_guided_boundary_prompts.py`
- Modify: `web/src/GuidedProductionWorkspace.jsx`

**Interfaces:**
- No new production interface.
- `/plan/attempts`, `/image-attempts`, `/prompt-attempts` must return a list even when one Job is `BLOCKED`; an unsupported state raises an explicit error for that entry only, never a silent omission.

- [ ] **Step 1: Write the failing cross-entry API test**

```python
def test_attempt_lists_survive_blocked_jobs(api, blocked_plan_job, blocked_image_job, blocked_prompt_job):
    for path in ('/plan/attempts', '/image-attempts', '/prompt-attempts'):
        response = api.get(base + path)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
        assert any(row['status'] in ('UNKNOWN', 'FAILED') for row in response.json())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_guided_web.py -k attempt_lists_survive`
Expected: FAIL because one blocked Job currently makes a list endpoint raise.

- [ ] **Step 3: Implement per-entry compatibility errors**

Projection helpers must map supported states and raise `ValueError('Unsupported <entry> status: <state>')` only when called for that entry. List endpoints continue returning other rows.

- [ ] **Step 4: Update documentation**

README:
- Guided storyboard/image/prompt are now persistent Jobs.
- The Worker or `studio web` embedded worker must be running.
- Unknown image/prompt requests remain blocked until checked; never blindly resubmit.
- Historical rows remain readable; new paid attempts do not dual-write.
- Mention the explicit limits that remain (PAUSED, real paid recovery/UI path, E2/E3 still pending).

ROADMAP:
- Mark M1-E1 complete after merge; leave M1-E2/E3 pending.
- Record Studio/VBench test counts and the fake-provider evidence.

- [ ] **Step 5: Run the full verification**

Run: `uv run --extra dev --extra web pytest -q`
Expected: all Studio tests pass.

Run: `cd ../vbench && uv run --extra test pytest -q`
Expected: 8 passed.

Run: `cd ../ai-video-studio/web && npm ci && npm run build`
Expected: Vite production build succeeds.

- [ ] **Step 6: Commit**

```bash
git add README.md ROADMAP.md tests/test_guided_web.py tests/test_guided_images.py \
  tests/test_guided_boundary_prompts.py web/src/GuidedProductionWorkspace.jsx
git commit -m "docs: record guided paid job compatibility"
```

---

## Self-Review

- **Spec coverage:** E1 covers guided storyboard LLM, character image, boundary image, boundary prompt, persistent attempts, user ceilings, unknown blocking, compatibility projections, and UI polling. E2/E3 are explicitly deferred with named follow-up plans.
- **Type consistency:** `request_jobs.submit` is the only enqueue helper; all three projection helpers share the legacy field sets quoted above. `request_id` is the idempotency key; `attempt_id` is only projected from the latest Job attempt.
- **Review Focus:** every review focus line has a named test in its owning task.
- **Proportion:** the plan fixes interfaces and test expectations; handlers keep the provider and validation logic already in the codebase rather than reproducing it.

## Remaining Work After E1

- **E2:** `narration.plan` and `narration.synthesize` jobs; one `tts_segment` Job per line sharing a run; aggregate concatenation/backup/manifest; CLI waits and reports partial failures; reference `generate_frame` Job with immutable reference digests.
- **E3:** Director and continuity vision reviews as one `vision_review` Job per shot/pair, shared run, aggregated report directory, partial-failure visibility, and no automatic retry of unknown calls.
