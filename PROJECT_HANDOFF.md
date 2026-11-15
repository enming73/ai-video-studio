# AI Video Studio — Project Handoff
Date: 2026-11-15
Status: Roadmap approved; M0 local acceptance tooling implemented and accepted by
the user on 2026-11-15 within its documented scope. No paid testing was performed
or authorized. M1 has not started; its detailed design is the next proposed step.

## Goal
Build a complete AI filmmaking studio: one-click complete draft → optional manual
refinement → targeted regeneration → reliable export. The next product milestone
is a complete, editable, recoverable 60-second draft within an authorized local
budget policy. Preserve existing CLI, data and the guided precision workflow;
normal automatic-draft runs should not require per-shot human confirmation.

## Current planning entry points
- [ROADMAP.md](ROADMAP.md): M0 baseline → M1 reliability/costs → M2 editing model
  and snapshots → M3 automatic 60-second draft → M4 refinement → M5 longer films.
- [Product constraints](docs/superpowers/specs/2026-11-15-studio-product-direction-design.md):
  agreed principle, proposed technical defaults and implementation boundaries.
- [Development workflow](docs/development-workflow.md): scope control, testing,
  evidence, migration and milestone acceptance.
- [M0 plan](docs/superpowers/plans/2026-11-15-m0-acceptance-baseline.md):
  implementation record; [M0 evidence](docs/acceptance/m0-baseline.md) contains
  commands, artifacts, failure-coverage inventory and the human acceptance checklist.
  M0 verifies the existing local media workflow, not paid-provider or automatic-draft acceptance.

Historical plans are context, not instructions to resume unfinished work.
Review the current milestone before starting implementation.

## Stack
Python CLI (`uv run --env-file .env studio ...`), `studio/` package, FastAPI (`studio web --port 8765`), React/Vite (`cd web && npm install && npm run dev`, http://127.0.0.1:5173), SQLite, FFmpeg/FFprobe, MiniMax image-to-video, OpenAI-compatible vision LLM. Project data in `studio-data/`.

The shared `video-provider-core/` also integrates fal Flux 3 Max. The repository
is the current source of truth; the versioned release guides describe historical
increments and should not be mistaken for the complete current workflow.

## Current guided workflow
- Guided projects can review the LLM storyboard, character references, and cast
  before producing shared boundary frames. Adjacent shots reuse each boundary;
  both ends must be confirmed before generating the shot. Shots can be generated
  out of order when their boundaries are ready.
- Guided image-to-video choices include Mock, MiniMax-H3 (4–15 seconds), and
  fal Flux 3 Max (5–20 seconds), subject to account access and provider changes.
  Flux first and last frames are uploaded to fal storage: URLs are public by
  default. Configure `FAL_KEY` and a verified `FAL_USD_PER_SECOND` locally.
- Known remote requests can be recovered without submitting again. fal queue
  status and result requests use the app-level path
  `blackforestlabs/flux-3-max/requests/{id}`, not the model endpoint suffix.
- For a completed remote result stuck at `DOWNLOAD_PENDING`, the UI can trim a
  downloaded video to the **current shot duration** and register a separate
  asset, retaining the original. Or explicitly end the old attempt to allow
  regeneration; its record and reserved budget remain. Do not automatically
  resubmit ambiguous or pending paid requests.
- On 2026-11-08, an existing Flux task was queried via the corrected API and
  reported `COMPLETED` with a video URL; a full UI download/import and final
  film were not verified in that check. Automated tests do not prove account
  access, final billing, visual quality, or end-to-end paid operation.

## Releases
- v0.1–0.4: storyboard, character references, first frames, MiniMax I2V, video assembly.
- v0.5: `shot_assets`, `timeline_items`, `render_history` in SQLite; version selection, mixed rendering, old work preserved.
- v0.6–0.6.2: soundtrack, narration/TTS, music/ambience, SFX, SRT, subtitles, ducking, export.
- v0.7–0.7.1: React Studio, FastAPI, project/media browsing, preview asset cache fix.
- v0.8.0: editable timeline, SFX drag/resize/gain, undo/redo, persistence.
- v0.8.1: continuous shot playback, synchronized SFX audition, browser export.
- v0.9.0: offline technical Director analyzer using FFprobe/FFmpeg (duration, black/frozen frames, keyframes), JSON reports.
- v0.9.1: vision LLM shot reviews, keep/manual_review/regenerate_candidate, dry-run and explicit budgeted live review.
- v0.9.2: Director Workspace in React, evidence, scores, manual decisions.
- v0.9.3: Create → Plan → Generate integration in browser; dry-run and explicit paid confirmation.
- v0.9.4: continuity workbench with preceding shot tail / next shot head keyframes.
- v0.9.5: Continuity Vision Agent compares adjacent-shot boundary frames, suggests transition/prompt revisions; paid vision requires authorization. Latest source ZIP: `ai-video-studio-v0.9.5-continuity-vision-clean.zip`.

## Historical real project (2026-11-07)
Project ID `3a5b4d58f96b`, 60-second six-shot 3D animated rainforest/waterfall story. Characters: Luma (small gray elephant, yellow scarf), Pico (turquoise parrot), Fern (lime-green frog). Vision review: shots 2 and 5 `keep`, others `manual_review`; user personally finds footage largely good but shot 2→3 transition abrupt because Fern appears without an introduction. Proposed remedies: edit shot 3 to show Fern emerging from foliage (requires compatible new first frame for I2V), or bridge shot; do NOT overwrite old shot. Continuity analysis and targeted regeneration are distinct.

## CLI reference
```
uv run --env-file .env studio create --prompt 'Ocean sunrise' --duration 60 --provider minimax --budget 5
uv run --env-file .env studio plan PROJECT_ID --shot-length 10
uv run --env-file .env studio generate PROJECT_ID            # DRY RUN
uv run --env-file .env studio generate PROJECT_ID --live --budget 5  # PAID
uv run --env-file .env studio sync-assets PROJECT_ID
uv run --env-file .env studio director-analyze PROJECT_ID --frames 3
uv run --env-file .env studio director-reports PROJECT_ID
uv run --env-file .env studio web --port 8765
```
For exact current commands/options always inspect `studio/cli.py` and version READMEs. Do not assume CLI flags from earlier assistant prose; in v0.6.2 `audio-add` flags had been misdocumented as options although positional.

## Key architecture/safety decisions
- Python CLI/core is canonical business logic; web API wraps existing core, not parallel implementations.
- SQLite manages versions; old media/reports never overwritten by regeneration.
- Dry-run default, no cloud calls without explicit authorization; budget reservation/estimate is NOT guaranteed provider-side hard spend cap.
- Never auto-execute regeneration on a Director recommendation; keep evidence, uncertainty, and human decision.
- Avoid duplicate paid submissions after unknown network outcomes; task states in browser backend may be in-memory and lost on restart.
- ZIP releases must exclude `.venv`, `.env`, `studio-data`, `node_modules`, `.git`; a v0.7.1 ZIP once mistakenly included `.venv` and broke local Python. Clean archives thereafter.
- Never claim browser/API end-to-end tests when only compile/ZIP tests ran.

## Current status and next steps
The user approved the roadmap and authorized M0 in an isolated worktree. M0 was
accepted by the user on 2026-11-15 within its documented local-media scope.
The next step is a separately scoped and reviewed M1 plan,
not automatic continuation of historical release plans. The historical v0.9.5 ZIP
was not user-verified; earlier verification claims are not a substitute for
testing the current repository.

The 2026-11-15 baseline assessment ran Studio tests (183 passed), VBench tests
(8 passed), frontend helper tests (5 passed) and the frontend production build
(passed). These are historical results for that assessment, not evidence for
future modifications or for a live paid/browser end-to-end production run.

M0 implementation verification: 201 Studio tests passed (same existing
Starlette/httpx warning). A clean-commit CLI run at `cf508ca` generated three
60-second 720p exports in `/tmp/opencode/studio-m0-20261115-cf508ca`.
Original source hashes were preserved; only shot 3 was replaced and subsequent
subtitle-only editing preserved decoded audio. Every export passed a full
FFmpeg decode and its render manifest was checked against the selected asset.
The inputs are synthetic colors and tones; the user accepted M0 on 2026-11-15
within its documented scope. See the evidence document.

Before further paid testing, verify provider access, rates, image handling,
budget reservations and outstanding remote tasks. Back up the local SQLite
database and generated media; avoid uploading credentials, local data or media
to Git. Keep source files and historical attempts intact.

## Handoff to new chat
Provide this Markdown file and the current Git repo/commit. Preserve local `.env`
and `studio-data` securely without uploading API keys. Read the relevant source
before modifying it; use Git branches and `pytest`/`npm run build` checks. Do
not treat this handoff as a substitute for code inspection.
