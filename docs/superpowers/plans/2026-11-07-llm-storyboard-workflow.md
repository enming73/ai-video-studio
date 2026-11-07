# LLM Storyboard Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` if the user explicitly selects delegation. Steps use checkboxes for tracking.

**Goal:** Add an opt-in, resumable guided production workflow with budget-authorized LLM storyboards, non-destructive per-shot generation, human confirmations, and a gated render.

**Architecture:** Extend the existing Python Core/SQLite and reuse the asset registry and FFmpeg renderer; FastAPI is a thin wrapper and React provides the approved vertical workflow. Guided projects have separate durable workflow, draft and attempt state; legacy projects and routes retain existing behavior. Unknown provider capabilities fail closed rather than guessing.

**Tech Stack:** Python 3.11+, Typer, FastAPI, SQLite, React/Vite, FFmpeg/FFprobe, existing provider core.

**Spec:** `docs/superpowers/specs/2026-11-07-llm-storyboard-workflow-design.md`

## Global Constraints

- Preserve `.env`, SQLite, `studio-data`, media, history and Director reports; migrations are additive and releases exclude `.venv`, `.env`, `studio-data`, `node_modules`, `.git`.
- All affected paid requests default to dry run and require a fresh explicit positive budget and confirmation; estimates/reservations are not vendor-enforced limits. Never call paid services during tests.
- Do not develop Director Continuity Agent or alter existing legacy-project flows. No destructive Git operations or automatic commits.
- All source durations are integer seconds, as in existing SQLite schema. Validate exact total and actual verified allowed values, not just a guessed min/max.
- Check vendor/model capabilities against primary documentation before enabling them; model/pricing unavailable or unverified means disabled with explanation. No fabricated estimates.

## Review Focus

1. An impossible total or unsupported provider must make zero paid requests (Tasks 1–2).
2. Duplicate clicks or ambiguous submission must never initiate a second paid job (Task 4).
3. Concurrent edits, server restart and changing selected media must not silently preserve invalid approval (Tasks 3–4).
4. Generated media and render outputs must remain intact on failure or regeneration, including file-name collision (Tasks 4–5).
5. Legacy data and endpoints must remain usable; attempts to bypass guided gating must not render through the guided endpoint (Tasks 3, 5–6).

## File Structure

- Create `ai-video-studio/studio/provider_capabilities.py`: verified duration sets, feasibility and validation.
- Create `ai-video-studio/studio/guided_storyboard.py`: planning, estimates, budget authorization and draft validation.
- Create `ai-video-studio/studio/guided_workflow.py`: additive SQLite state, draft edits, shot confirmations and render eligibility.
- Create `ai-video-studio/studio/guided_generation.py`: persistent per-attempt authorization, submit/recovery and immutable asset paths.
- Modify `studio/core.py`, `studio/cli.py`, `studio/timeline.py`, `studio/webapp.py` only at existing integration seams.
- Create `web/src/GuidedProductionWorkspace.jsx`; connect from `web/src/ProductionWorkspace.jsx`, retaining legacy controls for legacy projects; adapt `web/src/style.css`.
- Create `tests/test_provider_capabilities.py`, `tests/test_guided_storyboard.py`, `tests/test_guided_workflow.py`, `tests/test_guided_generation.py`, `tests/test_guided_web.py`; update relevant existing tests and docs.

### Task 1: Verified capability and feasibility boundary

**Files:** `studio/provider_capabilities.py`, `tests/test_provider_capabilities.py`.

**Interfaces:** `capabilities(provider: str, model: str) -> dict` returns `verified`, `durations` (sorted integer list), and `source`; `validate_durations(total: int, durations: list[int], allowed: list[int]) -> None`; `feasible(total: int, allowed: list[int]) -> bool`.

- [ ] Write tests proving `[8, 12]` is valid for total 20 and allowed `[8, 12]`; `[8, 8]` and `[8, 13]` raise; total 21 is infeasible for `[8, 12]`; unknown model returns `verified=False` without a paid request. Include empty allowed set and invalid integer inputs.
- [ ] Run `cd ai-video-studio && uv run --extra dev pytest -q tests/test_provider_capabilities.py`; expect failure from missing module.
- [ ] Implement bounded dynamic-programming feasibility and exact membership validation. Verify current provider/model durations from primary vendor documentation and record source URLs; leave any unsupported or unverified model disabled. Use a documented local mock capability for tests.
- [ ] Rerun the command; expect all tests passing.

### Task 2: Budgeted LLM planning, including CLI

**Files:** `studio/guided_storyboard.py`, `studio/core.py`, `studio/cli.py`, `tests/test_guided_storyboard.py`, `tests/test_v03.py`.

**Interfaces:** `preview(pid: str, model: str, shot_length: int | None = None) -> dict`; `request_plan(pid: str, model: str, budget_usd: float, *, confirm_paid: bool, shot_length: int | None = None) -> dict`; draft shape `{character_bible: dict, shots: [{prompt: str, duration: int}]}`. Both use Task 1 validation. `request_plan` returns a draft only, without replacing existing shots.

- [ ] Write mocked-LLM tests for variable durations `[8,12]` summing to 20, incorrect sum, disallowed duration, malformed payload, unavailable capability, insufficient/absent budget, missing confirmation and dry run: assert no network for every rejected or preview case, and no shot writes on any failure. Cover explicitly supplied fixed length and existing `core.smart_plan(pid,10,model)` call compatibility.
- [ ] Run `cd ai-video-studio && uv run --extra dev pytest -q tests/test_guided_storyboard.py tests/test_v03.py`; expect guided tests to fail.
- [ ] Implement preview and request: send total and allowed durations to LLM; validate response before returning; report unknown price as unknown, accept only an explicit positive reservation and warn it is not a provider cap. Disable existing CLI `smart-plan` live execution without explicit `--live` and `--budget`; with no `--shot-length`, use variable mode; make fixed mode opt-in. Preserve existing fixed-length Core signature for programmatic callers but route CLI through the budget guard.
- [ ] Rerun tests; expect pass. Confirm no test uses a real credential or network.

### Task 3: Durable draft and confirmation state

**Files:** `studio/guided_workflow.py`, `studio/core.py`, `studio/timeline.py`, `tests/test_guided_workflow.py`.

**Interfaces:** `create(pid: str) -> dict`; `get(pid: str) -> dict`; `save_draft(pid: str, draft: dict, revision: int) -> dict`; `confirm_storyboard(pid: str, revision: int) -> dict`; `confirm_shot(pid: str, seq: int, asset_id: str, revision: int) -> dict`; `render_ready(pid: str) -> bool`. Return workflow revision and per-shot selected asset/confirmation in `get`.

- [ ] Test new guided project marker, restart via fresh SQLite connection, stale-revision rejection, per-card add/delete/edit before storyboard confirmation, total validation, atomic confirmation, and rejection of replacing existing project shots. Confirm shot 1 only with its existing selected asset; reject shot 2 before shot 1. After both are confirmed, change shot 1 description or selected asset and assert only shot 1 loses approval; viewing alone leaves approvals unchanged. Unknown/missing assets never confirm. Existing legacy rows remain unchanged.
- [ ] Run `cd ai-video-studio && uv run --extra dev pytest -q tests/test_guided_workflow.py`; expect failure.
- [ ] Implement additive workflow/draft/shot-confirmation tables, transactional revision checks and transactional storyboard materialization. Do not change legacy project behavior. Hook guided description/selection mutations into confirmation invalidation, including existing API/Core paths, using the same SQLite transaction.
- [ ] Rerun tests; expect pass. Add a test for direct legacy selection/edit endpoints on a guided project to prove they cannot leave a stale approval.

### Task 4: Non-destructive per-shot generation and recovery

**Files:** `studio/guided_generation.py`, `studio/timeline.py`, `tests/test_guided_generation.py`.

**Interfaces:** `preview(pid: str, seq: int) -> dict`; `submit(pid: str, seq: int, budget_usd: float, *, confirm_paid: bool, request_id: str) -> dict`; `attempt(pid: str, attempt_id: str) -> dict`; `recover(pid: str, attempt_id: str, remote_id: str) -> dict`. Use Task 3 workflow state and existing provider `submit`/`poll` APIs.

- [ ] Write tests with fake provider and local temporary files: preview is side-effect-free; absent confirmation, budget, verified rate or earlier-shot confirmation prevents submit; concurrent/repeated same request ID submits at most once; crash after submit begins leaves `SUBMITTING` and blocks retry until explicit recovery; independent regeneration creates two distinct files and registered assets; collision/failure does not alter existing bytes; later confirmed shots remain confirmed.
- [ ] Run `cd ai-video-studio && uv run --extra dev pytest -q tests/test_guided_generation.py`; expect failure.
- [ ] Implement durable per-attempt reservation and state before remote submission, unique request ID and unique attempt directory; never reuse fixed `clips/{shot_id}.mp4`. Persist remote ID, support safe polling/download recovery without automatic resubmission, verify media before registration, and use existing `timeline` registry without auto-confirmation. Reserve against project and per-request budget transactionally; retain ambiguous reservations. Local mock follows the same unique-file lifecycle without paid calls.
- [ ] Rerun tests; expect pass. Verify provider API calls are stubbed and that neither failed downloads nor duplicate requests overwrite existing media.

### Task 5: Guided API and server-side render gate

**Files:** `studio/webapp.py`, `studio/guided_workflow.py`, `tests/test_guided_web.py`.

**Interfaces:** Add guided routes under `/api/projects/{pid}/guided`: `GET /`, `POST /plan/preview`, `POST /plan`, `PUT /draft`, `POST /storyboard/confirm`, `POST /shots/{seq}/preview`, `POST /shots/{seq}/generate`, `POST /shots/{seq}/confirm`, `GET /attempts/{attempt_id}`, `POST /attempts/{attempt_id}/recover`, `POST /render`. Request bodies carry the inputs and revision/request IDs from Tasks 2–4. Create a guided project via explicit `guided: true` on the existing creation endpoint; default false.

- [ ] Write API tests with FastAPI TestClient and fake LLM/provider for dry-run default, missing paid confirmation/budget, stale revision, project/shot ownership, direct shot-order bypass, missing or changed asset, incomplete render and complete render. Assert guided render refuses incomplete approval at the backend, and legacy routes retain their existing behavior.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_web.py`; expect failure.
- [ ] Implement thin request validation and wrappers around Tasks 2–4. Guided render checks durable approvals and uses existing non-overwriting selected-asset render/export; keep existing legacy render routes unchanged. Validate gate and render snapshot atomically enough that an intervening edit cannot render an unapproved selection; reject stale revisions.
- [ ] Rerun tests; expect pass.

### Task 6: React vertical guided workflow

**Files:** `web/src/GuidedProductionWorkspace.jsx`, `web/src/ProductionWorkspace.jsx`, `web/src/style.css`.

**Interfaces:** `GuidedProductionWorkspace({pid, onCreated, onPlanned})` consumes Task 5 routes; existing component retains original controls for legacy projects and offers explicit guided creation.

- [ ] Add focused UI tests using the existing test setup if present; if none exists, add minimal Vite-compatible test tooling only if available without disrupting dependencies. Cover card editing and duration errors, disabled confirmation, paid confirmation cancellation, locked next shot, invalidated prior shot, historical asset selection and render gate. If tooling cannot run, record manual steps and limitation.
- [ ] Build the vertical steps: creation; plan preview with model, estimate and per-call consent; editable individual cards with exact total and validation; whole-storyboard confirmation; current-shot preview, historical versions and per-attempt consent; final render. Show unknown cost and ambiguous submission state explicitly. Reload durable state after refresh; do not store confirmation only in React.
- [ ] Run `cd ai-video-studio/web && npm run build`; expect successful Vite build. Exercise the local mock workflow manually if environment permits, including browser refresh, and record whether the browser was actually tested.

### Task 7: Documentation and verification

**Files:** `README.md`, a new versioned guide if a release version is agreed, relevant tests.

- [ ] Document guided vs legacy scope, CLI fixed/variable planning, capability provenance, dry-run/authorization and cost limitations, non-destructive history, ambiguity recovery and local-only API security. Do not invent a new release number.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q`; record results. Run `cd vbench && uv run --extra test pytest -q` when available; record failures or missing dependencies.
- [ ] Run `cd ai-video-studio/web && npm run build`; inspect `git status --short` and review the diff for credentials, runtime data and unintended changes. If a release archive is requested later, separately inspect its file list for excluded paths.
- [ ] Report exact tests/build and any unverified live-provider, browser, FFmpeg, deployment or migration scenarios; never imply paid end-to-end validation without explicit authorization.
