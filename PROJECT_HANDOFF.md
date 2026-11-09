# AI Video Studio — Project Handoff
Date: 2026-11-09
Status: Current development direction is paused. Future work may explore finer
details and production workflows, but no next scope or schedule is agreed.

## Goal
Build a complete AI filmmaking studio: create story → plan storyboard → generate character-consistent images/video → manage versions → edit sound/timeline → Director AI review → human-authorized targeted regeneration → export. Preserve existing CLI and data.

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
The user has paused the current development direction. Do not assume a new
release or automatically continue the previous roadmap. A later phase may
research finer details and production flows; first clarify the desired scope,
then inspect the current code and actual project state. The historical v0.9.5
ZIP was not user-verified; earlier verification claims are not a substitute for
testing the current repository.

Before further paid testing, verify provider access, rates, image handling,
budget reservations and outstanding remote tasks. Back up the local SQLite
database and generated media; avoid uploading credentials, local data or media
to Git. Keep source files and historical attempts intact.

## Handoff to new chat
Provide this Markdown file and the current Git repo/commit. Preserve local `.env`
and `studio-data` securely without uploading API keys. Read the relevant source
before modifying it; use Git branches and `pytest`/`npm run build` checks. Do
not treat this handoff as a substitute for code inspection.
