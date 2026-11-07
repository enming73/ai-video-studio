# AI Video Studio v0.6.1 + VBench shared Provider Core

This repository contains the Studio v0.6.1 codebase, VBench, and their shared
provider core. See [README-v0.6.1.md](README-v0.6.1.md) for narration and TTS,
[README-v0.6.md](README-v0.6.md) for sound post-production, and
[README-v0.5.md](README-v0.5.md) for the asset registry and timeline. The
legacy workflows and caveats below originated in v0.2.

## Layout
- `video-provider-core/`: shared MiniMax, Wan, Flux implementations and secure media downloader
- `vbench/`: original benchmark, now importing shared providers through a compatibility module
- `ai-video-studio/`: Studio CLI with cloud task persistence and mock mode

## Install
```bash
cd vbench && uv sync --extra test
cd ../ai-video-studio && uv sync --extra dev
```
Both projects use editable `../video-provider-core` uv source. Keep the sibling directories together.
Use `ai-video-studio/.env.example` as a template for local configuration; do not
commit actual credentials. Back up `studio-data` separately, especially paid
assets and the database, before migrations or changing installations.

## Tests
```bash
cd ai-video-studio && uv run --extra dev pytest -q
cd ../vbench && uv run --extra test pytest -q
```

## Narration, sound and subtitles

On an existing project, generate an editable per-shot narration script, then
produce voice audio and shot-aligned SRT subtitles. Planning and TTS are dry
runs unless `--live` is supplied; live requests may incur provider charges.
Configure the LLM and TTS credentials as described in
[README-v0.6.1.md](README-v0.6.1.md), and review the script before synthesis.

```bash
cd ai-video-studio
uv run --env-file .env studio narration-plan PROJECT_ID --language zh-CN --model gpt-4o-mini --live
uv run studio narration-show PROJECT_ID
# Edit studio-data/PROJECT_ID/narration/script.json before TTS.
uv run --env-file .env studio narration-tts PROJECT_ID --voice alloy --model gpt-4o-mini-tts --live
uv run studio narration-attach PROJECT_ID
uv run studio export-film PROJECT_ID --source selected --burn-subtitles
```

`narration-attach` integrates the generated audio and subtitles into the
soundtrack while preserving existing music and ambience settings. FFmpeg is
required; burning subtitles requires a build with libass and re-encodes video.
Subtitles follow shot boundaries rather than individual words. The sound mixer
uses fixed gains rather than voice-aware ducking. See the versioned guides for
dry-run commands, TTS duration limits, and export options.

## Studio mock
```bash
cd ai-video-studio
uv run studio create --prompt 'Ocean sunrise' --duration 20 --provider mock --budget 0
uv run studio plan PROJECT_ID
uv run studio generate PROJECT_ID
uv run studio render PROJECT_ID
```

## MiniMax paid mode
Set `MINIMAX_API_KEY` and a **verified** `MINIMAX_USD_PER_SECOND` in environment (or `.env`). The inherited MiniMax-H3 endpoint and payload were taken directly from the supplied VBench; verify they are valid for your account before live submission. The code does not validate vendor model limits beyond the generic 5–15 second planning bound.
```bash
uv run studio create --prompt 'Ocean sunrise' --duration 60 --provider minimax --budget 5
uv run studio plan PROJECT_ID --shot-length 10
uv run studio generate PROJECT_ID                    # DRY RUN; no cloud calls
uv run studio generate PROJECT_ID --live --budget 5   # REAL PAID CALLS
uv run studio status PROJECT_ID
uv run studio render PROJECT_ID
```
Cloud provider clips must match requested duration within 0.25 seconds and be compatible for concatenation. For differing codecs, frame rates or resolutions, a normalization stage is still needed; v0.2 does not yet implement it.

## Ambiguous submissions
When a submission fails after its state is persisted as `SUBMITTING`, **never automatically resubmit**. Find the remote task ID on the vendor dashboard and attach it:
```bash
uv run studio attach PROJECT_ID SHOT_ID REMOTE_TASK_ID --confirm
uv run studio generate PROJECT_ID --live
```
The DB is migrated additively from v0.1. `STUDIO_HOME` must point to the same original studio-data directory to reuse existing projects.

## Caveats
- Budget is a conservative estimated reservation based on a manually verified per-second rate; not a vendor invoice. Failed and ambiguous submissions retain reservations.
- No automatic retries of ambiguous submissions. No parallel generation or automated QA scoring yet.
- Vendor APIs, model availability, and download hosts may change. Live mode requires verification.
- Do not upload `.env`, credentials, DB or generated videos.
