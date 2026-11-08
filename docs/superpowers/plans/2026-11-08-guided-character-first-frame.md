# Guided Character and First-Frame Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` only when the user explicitly requests delegation. Steps use `- [ ]` checkboxes.

**Goal:** Add opt-in character styling and first-frame approval to guided projects, with immutable lineage and actual first-frame-to-video generation.

**Architecture:** Extend the existing `studio` Core and additive SQLite tables; React and FastAPI remain adapters, with authoritative gates in Core. Capture immutable references from character version → first frame → video attempt → selected render; do not replace the legacy CLI manifest. New guided projects use the extended workflow; existing guided projects explicitly opt in and retain all old assets and decisions. Build in dependent vertical slices: persistence, paid image generation, actual I2V, then UI and compatibility verification.

**Tech Stack:** Python 3.11+, SQLite, FastAPI/Pydantic, React/Vite, httpx, FFmpeg/FFprobe, existing OpenAI-compatible image configuration and shared video providers.

**Spec:** `docs/superpowers/specs/2026-11-08-guided-character-first-frame-design.md`

## Global Constraints

- Preserve user SQLite, `.env`, `studio-data`, generated media/history and Director reports; never delete or overwrite old assets. Archive exclusions: `.venv`, `.env`, `studio-data`, `node_modules`, `.git`.
- No paid API call without explicit per-request budget and confirmation; preview defaults to Dry Run. Reservations are not provider-enforced spending caps, and failures can incur charges.
- Persist request IDs and uncertain states before paid calls; do not auto-retry ambiguous submissions. Never use a paid service during tests without separate user authorization.
- New guided projects use the extended workflow; pre-existing guided projects continue their existing behavior until explicit opt-in; never automatically alter legacy projects.
- Preserve previous approved workflow: changing one character affects only associated shots; a previously approved old video remains preserved and requires a deliberate keep-old decision before render.
- Verify actual model-specific image endpoints, I2V payload and duration restrictions from first-party docs; unknown means blocked, not guessed. Never silently substitute T2V for I2V.
- Keep existing CLI/legacy API operational; enforce gates server-side. Do not start Director Continuity work. Do not commit, push, merge, install external dependencies or invoke paid APIs without appropriate authorization.

## Review Focus

1. An existing project with a confirmed storyboard and paid videos is opted in: no shot, media, selection, approval, reference manifest or report disappears; provenance starts as `unknown` (Tasks 1, 6).
2. Image upload includes an oversized, forged-format, path traversal or another project's image ID: reject without storing an unsafe path or changing state (Tasks 2, 5).
3. Concurrent paid requests, lost response or process restart: retain the attempted request, block automatic resubmission and permit explicit recovery without another charge (Tasks 3, 4).
4. Character A changes with shots `[A]`, `[B]`, `[A,B]`, and `[]`: only A-linked frames/videos become stale; old files remain and renderer blocks until the user decides (Tasks 1, 6).
5. An unsupported I2V model, mismatched duration or malicious direct API call: fail before network submission; the actual outbound payload for a supported model includes the exact confirmed first-frame bytes (Tasks 4, 5).

## File Structure

- Create `ai-video-studio/studio/guided_characters.py`: opt-in activation, versioned roles, shot-role links and approvals.
- Create `ai-video-studio/studio/guided_images.py`: safe image storage, generation requests, immutable image versions and first-frame approval/lineage.
- Modify `ai-video-studio/studio/guided_storyboard.py`: structured character and per-shot cast suggestions without losing existing drafts.
- Modify `ai-video-studio/studio/guided_workflow.py`, `guided_generation.py`, `timeline.py`, `webapp.py` and `guided_web.py`: authoritative opt-in, approvals, video attempts, render gates and routes.
- Modify `video-provider-core/src/video_provider_core/providers.py`: explicit image-to-video interface; preserve existing text-to-video calls.
- Create `web/src/GuidedCharacters.jsx` and `web/src/GuidedFirstFrames.jsx`; integrate into `GuidedProductionWorkspace.jsx` and existing styles.
- Add `tests/test_guided_characters.py`, `tests/test_guided_images.py`, `tests/test_guided_i2v.py`; extend `tests/test_guided_web.py`, `tests/test_guided_workflow.py` and frontend helper tests.
- Update `docs/guided-workflow.md`, `.env.example` only with documented variable names, and version guide if the user later requests a release.

### Task 1: Durable cast, opt-in and precise invalidation

**Files:** Create `studio/guided_characters.py`, `tests/test_guided_characters.py`; modify `studio/core.py`, `studio/guided_workflow.py`, `studio/timeline.py`.

**Interfaces:** `activate(pid: str, revision: int) -> dict`; `get(pid: str) -> dict`; `save_character(pid: str, character_id: str | None, name: str, description: str, revision: int) -> dict`; `confirm_character(pid: str, character_id: str, image_id: str, revision: int) -> dict`; `set_shot_characters(pid: str, seq: int, character_ids: list[str], revision: int) -> dict`; `keep_old_video(pid: str, seq: int, asset_id: str, revision: int) -> dict`. Every mutation uses `BEGIN IMMEDIATE` and checks the shared workflow revision in the same transaction.

- [ ] Write tests: enabling an existing project preserves its exact shots, `timeline_items`, `shot_assets`, reports, legacy manifest and original confirmation rows; another legacy project is unchanged. Restart and reload retains opt-in and role state. Reject unknown/cross-project character IDs, stale revision, duplicate IDs and duplicate names; accept an empty project cast only after explicit user confirmation.
- [ ] Write test for A/B/A+B/no-role shots: after replacing A's confirmed image, A becomes unconfirmed; only A-linked frames become stale and only their selected old videos require explicit `keep_old_video`; B-only and no-role shots retain approval. `render_ready` and `timeline.render` reject unresolved old-video decisions, then accept exact retained asset IDs; files are unchanged.
- [ ] Run `cd ai-video-studio && uv run --extra dev pytest -q tests/test_guided_characters.py`; expect failure for missing module.
- [ ] Add idempotent schema migrations for opt-in state, stable role IDs, immutable role/shot association versions, role image selection/confirmation, frame dependencies and explicit old-video decisions. Seed image and frame records directly in Task 1 tests until Tasks 2 and 4 supply their public creation APIs. For old guided projects initialize as disabled; new guided projects initialize enabled. Add invariant checks to storyboard confirmation and render paths without changing un-enabled projects. Store draft-shot role IDs in `save_draft` before shots exist; `set_shot_characters` updates materialized confirmed-storyboard shots. Preserve unknown extra fields and existing draft formats.
- [ ] Run the focused test command; expect pass. Verify no legacy manifest or data file is rewritten by activation.

### Task 2: Image versions and safe browser upload

**Files:** Create `studio/guided_images.py`, `tests/test_guided_images.py`; modify `studio/guided_web.py`, `studio/webapp.py`.

**Interfaces:** `import_character_image(pid: str, character_id: str, filename: str, content: bytes, revision: int) -> dict`; `list_character_images(pid: str, character_id: str) -> list[dict]`; `image_path(pid: str, image_id: str) -> Path`. Add multipart `POST /api/projects/{pid}/guided/characters/{character_id}/images` and scoped `GET /api/projects/{pid}/guided/images/{image_id}`; upload body has image bytes and `revision`, not a server path.

- [ ] Write tests for valid PNG/JPEG/WebP, multiple versions and retained byte hashes after changing selection. Test the 20 MiB limit while streaming from `UploadFile`, before materializing the whole image in application memory; test unsupported/forged signature, `../../` filename, symlink/path escape, empty input, cross-project access and stale revision. Confirm response never exposes arbitrary filesystem paths.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_images.py`; expect failure.
- [ ] Implement streaming size-limited upload, content validation, generated server-side UUID file names, exclusive file creation in project-owned storage and additive image metadata. Reuse existing `references` validation where safe; do not change `manifest.json` or overwrite content-addressed legacy assets. Register new image version in the same logical operation as the workflow revision, cleaning up only a brand-new unregistered temporary file on failure.
- [ ] Run the focused command; expect pass. Test through `TestClient`, not just helper functions.

### Task 3: Character proposals and budgeted image creation

**Files:** Modify `studio/guided_storyboard.py`, `studio/guided_web.py`; extend `studio/guided_images.py`, `tests/test_guided_images.py`, `tests/test_guided_storyboard.py`.

**Interfaces:** `preview_character(pid: str, character_id: str, model: str) -> dict`; `generate_character(pid: str, character_id: str, model: str, budget_usd: float, *, confirm_paid: bool, request_id: str, revision: int) -> dict`; `image_attempt(pid: str, attempt_id: str) -> dict`. Extend the current single LLM plan response with normalized characters `{id,name,description}` and each draft shot's `character_ids`; old `character_bible` inputs remain readable and never trigger a paid call automatically.

- [ ] Write tests for one LLM response containing two distinct roles and cast links, duplicate/unknown role IDs, explicitly empty cast and old draft with no role IDs. Assert existing drafts are preserved and can be manually linked without calling an LLM. Reject hallucinated IDs rather than silently assigning them.
- [ ] Write tests for image preview, missing/NaN/zero budget, missing confirmation, repeated and concurrent request IDs, network timeout and restart. Assert zero requests on preview/rejection; exactly one mocked POST for repeated ID; ambiguous state blocks automatic retry; earlier images remain intact.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_images.py tests/test_guided_storyboard.py`; expect new tests to fail.
- [ ] Verify first-party documentation for configured OpenAI-compatible model's text-to-image `/images/generations` and `/images/edits` capabilities and record citations in `docs/research/2026-11-08-image-i2v-capabilities.md`. Implement generation with separate explicit capability flags; disabled if the selected model/API is unverified. Reuse existing `STUDIO_IMAGE_API_KEY`/`STUDIO_IMAGE_BASE_URL`, but never infer pricing or actual availability. Create immutable attempt/result records before network, with per-request authorization and explicit manual resolution for uncertain outcomes.
- [ ] Run the focused tests; expect pass. All network calls use fake transport, never real keys.

### Task 4: First-frame generation, provenance and confirmation

**Files:** Extend `studio/guided_images.py`, `studio/guided_web.py`, `tests/test_guided_images.py`; modify `studio/guided_workflow.py`.

**Interfaces:** `preview_frame(pid: str, seq: int, model: str) -> dict`; `generate_frame(pid: str, seq: int, model: str, budget_usd: float, *, confirm_paid: bool, request_id: str, revision: int) -> dict`; `confirm_frame(pid: str, seq: int, image_id: str, revision: int) -> dict`. Image metadata stores exact shot prompt/association revision plus selected role-image IDs and hashes, not just paths or names.

- [ ] Write tests: missing role approval or storyboard confirmation blocks preview and generation; one/two/no-role shots use exact confirmed references (no-role uses documented text-to-image path); no stale or cross-project reference is accepted. Confirmation requires an existing selected file with matching hash. Regeneration creates a new version; an old stale frame can only become approved through an explicit user action, never automatically.
- [ ] Write tests for unknown/failed/duplicate paid submissions, concurrent changes during a request, old frame retention and process restart. A result produced from outdated input is retained but is not silently selected or approved.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_images.py`; expect new tests to fail.
- [ ] Implement previews, immutable input snapshots, separate generation/edit request construction and persistence/approval gates. Render a clear stale reason and image version/provenance in `get`; do not overwrite the old `references/manifest.json`.
- [ ] Run the focused tests; expect pass. Check that all role inputs use explicit IDs, not substring matching.

### Task 5: Actual first-frame video generation

**Files:** Modify `video-provider-core/src/video_provider_core/providers.py`, `studio/provider_capabilities.py`, `studio/guided_generation.py`, `studio/guided_web.py`; create `tests/test_guided_i2v.py`.

**Interfaces:** Add provider `submit_i2v(prompt: str, duration: int, first_frame: bytes, mime_type: str) -> str` only where verified; add `i2v_capabilities(provider: str, model: str) -> dict` returning `verified`, exact `durations`, `source`, input modes. Extend guided video `preview`/`submit` for enabled projects to require confirmed frame ID and persist its hash/ID with every attempt; existing non-enabled project T2V interface stays unchanged. Add `PUT /api/projects/{pid}/guided/video-model` to select a verified provider/model under revision control.

- [ ] Write tests: the selected confirmed frame bytes, not only its ID or text prompt, occur in the mocked provider payload; stale/missing/altered frame, invalid image, unsupported model or incompatible duration creates no paid submission. Switching a video model does not silently modify shot duration or existing assets. Existing T2V users and projects not opted in retain prior behavior.
- [ ] Write tests for duplicate request IDs, ambiguous submit/recovery, generated video provenance and two unique video versions; after a role change the unrelated shot's approval remains and the affected old selected video cannot render before a keep-old decision.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_i2v.py`; expect failure.
- [ ] Verify the MiniMax H3 first-frame API and any Flux I2V endpoint against first-party docs, including model IDs, duration, size limits, auth and actual payload. Record evidence in the Task 3 research document. Implement only verified adapters, at minimum the supported MiniMax route if evidence remains valid; list other choices as unsupported until verified. Use the existing safe remote-task lifecycle, verified rate, project/run budgets, FFprobe checks and unique output files. Do not claim live verification from mocked tests.
- [ ] Run focused tests and `cd vbench && uv run --extra test pytest -q`; expect pass.

### Task 6: FastAPI and React guided stages

**Files:** Create `web/src/GuidedCharacters.jsx`, `web/src/GuidedFirstFrames.jsx`; modify `web/src/GuidedProductionWorkspace.jsx`, `web/src/ProductionWorkspace.jsx`, `web/src/style.css`, `studio/guided_web.py`, `tests/test_guided_web.py`.

**Interfaces:** `/guided/characters`, `/guided/characters/{id}`, `/guided/characters/{id}/confirm`, `/guided/shots/{seq}/characters`, `/guided/shots/{seq}/frames/preview`, `/guided/shots/{seq}/frames`, `/guided/shots/{seq}/frames/{image_id}/confirm`, `/guided/shots/{seq}/keep-old-video` delegate to Tasks 1–5. Mutations accept shared `revision`; paid bodies additionally require `confirm_paid`, `budget_usd`, and `request_id`. Existing `GET /guided/` includes `character_stage_enabled`, roles, links, frame/video version status and provenance.

- [ ] Write API tests for an existing guided project explicitly opting in, editing and confirming two roles with uploaded images, editing draft links, confirming the whole storyboard, confirming a frame, selecting an I2V model and generating with a fake transport. Check unauthorized/missing approval routes make no network request; error responses do not include credentials or local paths.
- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q tests/test_guided_web.py`; expect new tests to fail.
- [ ] Build the agreed vertical flow with individual character and shot cards, candidate upload/generation/selection, explicit confirmations, cast editing, per-call budget dialogs, stale/version indicators, provider-switch guidance and keep-old-video decision. Reload all state from server on project switch/restart; do not lose unsaved cards on a recoverable request error. Preserve old UI for non-enabled projects; no UI button silently invokes a paid API.
- [ ] Add deterministic frontend tests for confirmation gates, cast changes, stale markers, per-call confirmation cancellation and saved draft recovery. Run those tests and `cd ai-video-studio/web && npm run build`; expect pass. Record browser manual testing separately; build is not browser E2E.

### Task 7: Documentation, full regression and handoff

**Files:** `docs/guided-workflow.md`, `.env.example`, tests and Task 3 research note.

- [ ] Document new vs opted-in projects, image model setup, upload privacy, independent paid budgets, data retention, uncertain-request recovery, supported I2V models and first-frame/video lineage. State clearly that old T2V footage has unknown first-frame provenance and budget is not a provider-side hard cap. Update `.env.example` names only; never copy the real `.env`.- [ ] Run `cd ai-video-studio && uv run --extra dev --extra web pytest -q`; run `cd vbench && uv run --extra test pytest -q`; run `cd ai-video-studio/web && npm run build` plus the frontend tests. Read outputs and list any failures or warnings by name.
- [ ] Run `git diff --check` and inspect untracked files and staged paths if staging is requested. If an upgrade archive is later requested, inspect its contents for all excluded directories and secrets.
- [ ] Report tested mock/FFmpeg behavior separately from unverified real LLM, image model, I2V provider, browser and data-migration scenarios. Leave all user data and worktrees intact; ask before commits, merges, pushes or releases unless the user has separately authorized them.
