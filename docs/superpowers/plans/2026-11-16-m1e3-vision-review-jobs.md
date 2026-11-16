# M1-E3 Vision Review Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Director shot reviews and continuity pair reviews through persistent per-shot/per-pair Jobs sharing one Run, while preserving the existing CLI/API response shapes and keeping partial failures visible in the aggregated report.

**Architecture:** Add a `vision_review` Job kind and a `vision_jobs` batch helper. Each selected shot/pair becomes one Job with an immutable payload (report ID, seq/pair, model, base URL, frame paths, prompt, per-unit ceiling) and one reservation. A single handler dispatches to `director_vision.execute_shot` or `continuity_vision.execute_pair`. The CLI/API enqueues a batch, waits through `job_runner.run_until`, then assembles one `review.json` from every Job result/error. Unknown provider outcomes stay `BLOCKED` with retained reservations; no automatic retry.

**Tech Stack:** Python 3.11+, existing Job/Worker/ledger, SQLite/WAL, existing OpenAI-compatible HTTP client, pytest, fake HTTP transports.

**Spec:** `docs/superpowers/specs/2026-11-15-m1-reliability-design.md` §3, §4.2–§4.4, §5, §7; Roadmap M1.1–M1.4.

## Global Constraints

- Job/Attempt/Event is the only new truth; no new legacy vision-attempt tables or rows.
- Every paid vision review requires `live=True`, a positive budget, and an idempotency request ID.
- Unknown provider outcome: persist the attempt before the network call, mark `UNKNOWN_REMOTE_STATE`, set Job `BLOCKED`, retain the reservation, never auto-resubmit.
- Pricing is a user-provided per-shot/per-pair ceiling; use `pricing_version="user_ceiling:<request_key>"`.
- Money is integer micro-USD; times are UTC ISO strings.
- Preserve existing CLI/API URLs and report schema. Partial failures appear as `results[*].status='error'`; do not claim all shots succeeded.
- Tests never call real paid APIs; fake provider transports/frames.
- No M2 Scene/Timeline work, no resolution/quality changes.

## Review Focus

1. **Partial failure is hidden in the report** → Task 3 `test_director_review_marks_partial_failure`, `test_continuity_review_marks_partial_failure`.
2. **Repeating a live review creates a second paid batch** → Task 1 `test_director_review_replay_returns_same_review_and_jobs`, Task 2 `test_continuity_review_replay_returns_same_review_and_jobs`.
3. **One unknown provider call is retried** → Task 1 `test_director_unknown_shot_blocks_without_resubmit`, Task 2 `test_continuity_unknown_pair_blocks_without_resubmit`.
4. **Budget plan creates Jobs before checking the total ceiling** → Task 1 `test_no_jobs_when_total_ceiling_exceeds_budget`, Task 2 `test_no_jobs_when_pair_ceiling_exceeds_budget`.
5. **Aggregated report uses stale/mismatched frames or seqs** → Task 1 `test_director_aggregate_preserves_job_payload_order_and_source_ids`, Task 2 `test_continuity_aggregate_preserves_pair_mapping`.

---

### Task 1: Shared vision Jobs + Director per-shot reviews

**Files:**
- Create: `studio/vision_jobs.py`
- Create: `studio/handlers/vision_review.py`
- Modify: `studio/handlers/__init__.py`
- Modify: `studio/director_vision.py`
- Modify: `studio/cli.py:312-326`
- Modify: `tests/test_director_vision.py`
- Create: `tests/test_vision_jobs.py`

**Interfaces:**
- `vision_jobs._batch_key(kind, report_id, model, selections, ceilings)` → stable 32-hex.
- `vision_jobs.UnknownRemote`, `PreRequestFailure`, `PostResponseFailure`: the executor exception classes the handler catches. Do not invent a second set in `director_vision`/`continuity_vision`.
- `vision_jobs.enqueue_director(pid, report_id, model, base_url, budget_usd, per_shot_ceiling_usd, selected, request_id=None) -> dict`
  - Validates the existing `director_vision.plan` and `within_budget`; no Jobs when over budget.
  - Derives a stable `review_id = sha256(request_id).hexdigest()[:12]`.
  - Creates one `vision_review` Job per selected shot with target key `director:<report_id>:<seq>`, shared `run_key=f'vision:{batch_key}'`.
  - Payload: `kind='director'`, project id, report id, review id, seq, prompt, frame paths, source asset id, model, base URL, per-shot ceiling micro-USD.
  - Returns `{'review_id', 'run_id', 'job_ids', 'request_id', 'estimate'}`.
- `director_vision.execute_shot(payload) -> dict`
  - Revalidates every frame path is under `director-reports/<report_id>` and ≤5 MB; builds the existing content payload; POSTs once; normalizes; returns `{'seq', 'status':'reviewed', 'review':..., 'usage':..., 'source_asset_id':...}`.
  - Frame/path validation failures before HTTP → `PreRequestFailure`.
  - HTTP/transport/JSON ambiguity → `UnknownRemote`.
  - Post-response validation errors → `PostResponseFailure` (retained).
- `director_vision.assemble(pid, jobs, *, report_id, model, base_url, estimate, review_id, run_id, request_id) -> dict`
  - Sort Jobs by payload seq, build the existing report shape plus `review_id`, `run_id`, `job_ids`, `request_id`, and `partial = any(result.status == 'error')`.
  - `paid_api_calls = len(jobs)`; `results` contains successes and `status='error'` for FAILED/BLOCKED Jobs.
  - Write `director-reviews/<review_id>/review.json` atomically.
- `vision_review.execute(job, attempts, control)`: dispatch on `payload['kind']`, reserve the payload ceiling, call the executor, mark `UNKNOWN_REMOTE_STATE`/block for `vision_jobs.UnknownRemote`, release for `PreRequestFailure`, retain for `PostResponseFailure`/unexpected post-request errors.
- `director_vision.review(..., request_id=None)`: enqueue, `job_runner.run_until(job_ids)`, assemble. The returned report includes `review_id`, `run_id`, `job_ids`, `request_id`, and `partial`. Dry-run unchanged; `request_id` derived when omitted. Add `--request-id` to `director-review`.

- [ ] **Step 1: Write the failing enqueue/budget tests**

```python
def test_director_review_creates_one_job_per_shot(director_report, monkeypatch):
    result = director_vision.review(pid, report_id, 'model', live=True,
                                    budget_usd=1, per_shot_ceiling_usd=0.2,
                                    request_id='v1')
    assert len(result['job_ids']) == 2
    assert result['review_id']
    assert all(jobs.get(job_id)['kind'] == 'vision_review' for job_id in result['job_ids'])

def test_no_jobs_when_total_ceiling_exceeds_budget(director_report):
    with pytest.raises(ValueError, match='Budget insufficient'):
        director_vision.review(pid, report_id, 'model', live=True,
                               budget_usd=0.1, per_shot_ceiling_usd=0.2,
                               request_id='v1')
    with core.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='vision_review'").fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_vision_jobs.py -k 'one_job_per_shot or total_ceiling'`
Expected: FAIL with missing `vision_jobs` / synchronous review.

- [ ] **Step 3: Implement `vision_jobs.enqueue_director` and the handler**

Keep `director_vision.plan` as the validation source. Do not create the output directory until assembly. Use one shared Run for the batch.

- [ ] **Step 4: Add the failing Worker/aggregation test**

```python
def test_director_review_marks_partial_failure(director_report, monkeypatch):
    monkeypatch.setattr(director_vision, 'execute_shot',
                        lambda payload: (_ for _ in ()).throw(vision_jobs.UnknownRemote('timeout'))
                        if payload['seq'] == 2 else REVIEW_RESULT)
    result = director_vision.review(pid, report_id, 'model', live=True,
                                    budget_usd=1, per_shot_ceiling_usd=0.2,
                                    request_id='v1')
    assert result['results'][1]['status'] == 'error'
    assert result['paid_api_calls'] == 2
    with core.connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE request_key LIKE 'vision_review:%' ORDER BY created").fetchall()
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_vision_jobs.py -k partial_failure`
Expected: FAIL because no Job/report aggregation exists.

- [ ] **Step 6: Implement `execute_shot` and `assemble`, then run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_director_vision.py tests/test_vision_jobs.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add studio/vision_jobs.py studio/handlers/vision_review.py studio/handlers/__init__.py \
  studio/director_vision.py studio/cli.py tests/test_director_vision.py tests/test_vision_jobs.py
git commit -m "feat: enqueue director vision review jobs"
```

---

### Task 2: Continuity per-pair reviews

**Files:**
- Modify: `studio/vision_jobs.py`
- Modify: `studio/director_vision.py` (shared executor helpers if needed)
- Modify: `studio/continuity_vision.py`
- Modify: `studio/webapp.py:350-368`
- Modify: `tests/test_continuity_vision.py`
- Modify: `tests/test_vision_jobs.py`

**Interfaces:**
- `vision_jobs.enqueue_continuity(pid, report_id, model, base_url, budget_usd, per_pair_ceiling_usd, pairs, request_id=None) -> dict`
  - Uses `continuity_vision._targets` for valid ready pairs; no Jobs when over budget.
  - Target key `continuity:<report_id>:<from_seq>`; payload `kind='continuity'`, pair mapping, frame paths, prompts, source asset ids, per-pair ceiling.
  - Shared Run and derived `review_id`.
- `continuity_vision.execute_pair(payload) -> dict`
  - Revalidates the four frame paths under `continuity-reports/<report_id>`, POSTs once, normalizes, returns `{'from_seq','to_seq','status':'reviewed','review':...,'usage':...,'source_asset_ids':[...]}`.
- `continuity_vision.assemble(pid, jobs, *, report_id, model, estimate, review_id, run_id, request_id) -> dict`
  - Sort by `from_seq`; preserve pair mapping; errors for FAILED/BLOCKED; `paid_api_calls = len(jobs)`; add `review_id`, `run_id`, `job_ids`, `request_id`, and `partial`; write `continuity-vision-reviews/<review_id>/review.json`.
- `continuity_vision.review(..., request_id=None)`: enqueue, wait, assemble; dry-run unchanged; returned report includes `review_id`, `run_id`, `job_ids`, `request_id`, and `partial`. The web API request model gains an optional `request_id`; existing report fields are preserved.

- [ ] **Step 1: Write the failing continuity enqueue/replay tests**

```python
def test_continuity_review_creates_one_job_per_ready_pair(continuity_report, monkeypatch):
    result = continuity_vision.review(pid, report_id, 'model', live=True,
                                      budget_usd=1, per_pair_ceiling_usd=0.2,
                                      pairs=[2], request_id='c1')
    assert len(result['job_ids']) == 1
    assert result['review_id']

def test_continuity_review_replay_returns_same_review_and_jobs(continuity_report, monkeypatch):
    first = continuity_vision.review(pid, report_id, 'model', live=True,
                                     budget_usd=1, per_pair_ceiling_usd=0.2,
                                     pairs=[2], request_id='c1')
    second = continuity_vision.review(pid, report_id, 'model', live=True,
                                      budget_usd=1, per_pair_ceiling_usd=0.2,
                                      pairs=[2], request_id='c1')
    assert second['review_id'] == first['review_id']
    assert second['job_ids'] == first['job_ids']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_vision_jobs.py -k 'continuity and (creates or replay)'`
Expected: FAIL with missing `enqueue_continuity`.

- [ ] **Step 3: Implement continuity jobs and the web API request ID**

Keep `_targets` as the source of ready pairs and `normalize` unchanged. Add `request_id` to the Pydantic request model with a default derived value when absent. Preserve the returned report fields.

- [ ] **Step 4: Add the partial-failure and unknown tests**

```python
def test_continuity_review_marks_partial_failure(continuity_report, monkeypatch):
    monkeypatch.setattr(continuity_vision, 'execute_pair',
                        lambda payload: (_ for _ in ()).throw(vision_jobs.UnknownRemote('timeout'))
                        if payload['from_seq'] == 3 else PAIR_RESULT)
    result = continuity_vision.review(pid, report_id, 'model', live=True,
                                      budget_usd=1, per_pair_ceiling_usd=0.2,
                                      request_id='c2')
    assert result['results'][1]['status'] == 'error'
    assert result['paid_api_calls'] == 2
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_continuity_vision.py tests/test_vision_jobs.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/vision_jobs.py studio/director_vision.py studio/continuity_vision.py \
  studio/webapp.py tests/test_continuity_vision.py tests/test_vision_jobs.py
git commit -m "feat: enqueue continuity vision review jobs"
```

---

### Task 3: API/CLI compatibility and partial-failure visibility

**Files:**
- Modify: `studio/webapp.py:350-368`
- Modify: `studio/cli.py:312-326`
- Modify: `studio/vision_jobs.py`
- Modify: `tests/test_vision_jobs.py`
- Modify: `tests/test_director_vision.py`
- Modify: `tests/test_continuity_vision.py`

**Interfaces:**
- CLI `director-review --live` reports the aggregated report and `review_id`; if every Job is BLOCKED/FAILED it exits non-zero without claiming success.
- Web continuity review returns the same report shape with `review_id`, `results`, `paid_api_calls`, and `budget`.
- `assemble` includes failed/blocked Jobs as `status='error'` with a short reason and never fabricates a review object.

- [ ] **Step 1: Write the failing partial-failure CLI/API tests**

```python
def test_director_cli_partial_failure_exits_nonzero(runner, director_report, monkeypatch):
    # one shot succeeds, one times out
    result = runner.invoke(app, ['director-review', pid, report_id, '--live',
                                 '--budget', '1', '--per-shot-ceiling', '0.2'])
    assert result.exit_code != 0
    assert 'partial' in result.output.lower() or 'blocked' in result.output.lower()

def test_continuity_api_keeps_report_shape_with_one_failed_pair(api, continuity_report, monkeypatch):
    # one pair succeeds, one blocks
    response = api.post(f'/api/projects/{pid}/director/continuity-vision',
                        json={'report_id': rid, 'model': 'm', 'live': True,
                              'budget_usd': 1, 'per_pair_ceiling_usd': 0.2,
                              'request_id': 'c3'})
    assert response.status_code == 200
    assert any(r['status'] == 'error' for r in response.json()['results'])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_vision_jobs.py -k 'cli_partial or api_keeps'`
Expected: FAIL because partial failures currently exit 0 / no request_id field.

- [ ] **Step 3: Implement CLI/API reporting**

Preserve dry-run output. For live reviews, print the aggregated report and set a non-zero exit only for partial failures. Do not change the existing report field names.

- [ ] **Step 4: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_director_vision.py tests/test_continuity_vision.py tests/test_vision_jobs.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/webapp.py studio/cli.py studio/vision_jobs.py \
  tests/test_vision_jobs.py tests/test_director_vision.py tests/test_continuity_vision.py
git commit -m "fix: report partial vision review failures"
```

---

### Task 4: Regression and documentation

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `tests/test_director_vision.py`
- Modify: `tests/test_continuity_vision.py`
- Modify: `tests/test_vision_jobs.py`

**Interfaces:**
- No new production interface.
- Vision review dry-run output unchanged.
- Live reports always expose `review_id`, per-result statuses, and `paid_api_calls`.

- [ ] **Step 1: Add the final cross-report regression tests**

```python
def test_director_and_continuity_reviews_share_job_truth(director_report, continuity_report):
    director = director_vision.review(pid, director_report_id, 'm', live=True, budget_usd=1,
                                       per_shot_ceiling_usd=0.2, request_id='d1')
    continuity = continuity_vision.review(pid, continuity_report_id, 'm', live=True, budget_usd=1,
                                           per_pair_ceiling_usd=0.2, request_id='c1')
    assert director['review_id'] != continuity['review_id']
    assert all(jobs.get(job_id)['kind'] == 'vision_review' for job_id in
               director['job_ids'] + continuity['job_ids'])
```

- [ ] **Step 2: Run it to verify it fails or expose missing output**

Run: `uv run --extra dev --extra web pytest -q tests/test_vision_jobs.py -k share_job_truth`
Expected: FAIL if the two report types do not share the Job truth or IDs collide.

- [ ] **Step 3: Update documentation**

README:
- Director/continuity vision reviews are persistent Jobs.
- One Job per shot/pair, shared Run.
- Partial failures remain in the report; blocked unknowns retain reservations.
- CLI/API wait and report `review_id`; a standalone Worker is an alternative executor.
- E3 implemented in this branch; no real paid API tested.

ROADMAP:
- Mark E3 implemented in this branch / merge pending.
- Record Studio/VBench/build counts.

- [ ] **Step 4: Run full verification**

Run: `uv run --extra dev --extra web pytest -q`
Expected: all Studio tests pass.

Run: `cd ../vbench && uv run --extra test pytest -q`
Expected: 8 passed.

Run: `cd ../ai-video-studio/web && npm ci && npm run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add README.md ROADMAP.md tests/test_director_vision.py \
  tests/test_continuity_vision.py tests/test_vision_jobs.py
git commit -m "docs: record vision review job compatibility"
```

---

## Self-Review

- **Spec coverage:** E3 covers per-shot/per-pair vision Jobs, shared Runs, per-unit user ceilings, unknown blocking, no auto-retry, partial-failure reports, CLI/API compatibility, and report assembly. No M2 work.
- **Type consistency:** one `vision_review` kind; `kind` in payload distinguishes director/continuity; `review_id` is derived from the request ID; all report fields preserve existing names.
- **Review Focus:** every review focus line has a named test in its task.
- **Proportion:** the plan reuses existing frame validation and normalization, and specifies only the job boundary, payload, reservation, and aggregation decisions.
