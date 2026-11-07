# AI Video Studio — Project Handoff
Date: 2026-11-07
Status: Feature development paused at v0.9.5; v0.9.5 has NOT been user-verified.

## Goal
Build a complete AI filmmaking studio: create story → plan storyboard → generate character-consistent images/video → manage versions → edit sound/timeline → Director AI review → human-authorized targeted regeneration → export. Preserve existing CLI and data.

## Stack
Python CLI (`uv run --env-file .env studio ...`), `studio/` package, FastAPI (`studio web --port 8765`), React/Vite (`cd web && npm install && npm run dev`, http://127.0.0.1:5173), SQLite, FFmpeg/FFprobe, MiniMax image-to-video, OpenAI-compatible vision LLM. Project data in `studio-data/`.

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

## Known real project
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
User explicitly requested PAUSE on continuity/Director development and a new chat with context. Do not immediately implement v0.9.6. First ask what direction the user wants in the new chat. v0.9.5 ZIP was produced and archive integrity checked, but has not been confirmed working by user; prior versions through v0.9.4 were user-verified in conversation (some earlier claims ambiguous). If resuming, inspect actual current repository or ZIP and user test results, then prioritize requested scope.

## Handoff to new chat
Attach this Markdown file and the v0.9.5 source ZIP, or provide current Git repo/commit. Also preserve local `.env` and `studio-data` securely, without uploading API keys. Ask assistant to read project source before modifying it. Use Git branches/tags and `pytest`/`npm run build` checks. Do not treat the handoff document as a substitute for code inspection.
