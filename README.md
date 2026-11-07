# AI Video Studio v0.7 + VBench shared Provider Core

This repository contains Studio v0.7, VBench, and their shared provider core.
Studio includes a local React/Vite visual editor backed by FastAPI, alongside
the existing CLI. See [the v0.7 guide](ai-video-studio/README-v0.7.md) for the
editor and [README-v0.6.2.md](README-v0.6.2.md) for the audio timeline,
ducking and subtitle fonts; [README-v0.6.1.md](README-v0.6.1.md) for narration and TTS,
[README-v0.6.md](README-v0.6.md) for sound post-production, and
[README-v0.5.md](README-v0.5.md) for the asset registry and timeline. The
legacy workflows and caveats below originated in v0.2.

## Layout
- `video-provider-core/`: shared MiniMax, Wan, Flux implementations and secure media downloader
- `vbench/`: original benchmark, now importing shared providers through a compatibility module
- `ai-video-studio/`: Studio CLI and local visual editor with cloud task persistence and mock mode

## Install
```bash
cd vbench && uv sync --extra test
cd ../ai-video-studio && uv sync --extra dev
```
Both projects use editable `../video-provider-core` uv source. Keep the sibling directories together.
Use `ai-video-studio/.env.example` as a template for local configuration; do not
commit actual credentials. Back up `studio-data` separately, especially paid
assets and the database, before migrations or changing installations.

## Local visual editor

The v0.7 editor lets you browse existing projects, preview shots, select
registered original or I2V assets, and add, audition or remove sound effects
on a timeline. It reuses the existing project database and does not initiate
paid AI generation calls. Rendering and paid generation remain CLI workflows.

Start the API and the Vite development server in separate terminals:

```bash
cd ai-video-studio
uv sync --extra dev --extra web
uv run studio web --port 8765
```

```bash
cd ai-video-studio/web
npm install
npm run dev
```

Open <http://127.0.0.1:5173>. The Vite server proxies API requests to port
8765. The API has no authentication: keep it local and do not expose it to the
Internet. Audio files must be accessible on the backend machine. Preserve
`studio-data` when upgrading, or point `STUDIO_HOME` to the existing data.
See [the v0.7 guide](ai-video-studio/README-v0.7.md) for details and limitations.

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
Subtitles follow shot boundaries rather than individual words. See the versioned
guides for dry-run commands, TTS duration limits, and export options.

### Audio events, ducking and Chinese subtitles

Add existing sound-effect files to an independent timeline with a start time,
duration, gain and optional fades. The files are referenced, not copied, so
keep them in place. List events to find IDs for removal.

```bash
uv run studio audio-add PROJECT_ID --file /path/to/bird.wav --at 12.5 --duration 2.5 --gain 0.65 --fade-in 0.1 --fade-out 0.3
uv run studio audio-list PROJECT_ID
uv run studio audio-remove PROJECT_ID EVENT_ID
STUDIO_SUBTITLE_FONT='Noto Sans CJK SC' STUDIO_SUBTITLE_SIZE=24 \
  uv run studio export-film PROJECT_ID --source selected --burn-subtitles --ducking
```

`--ducking` is optional and experimental: FFmpeg `sidechaincompress` lowers
music in response to narration only when both tracks are configured. Subtitle
font defaults to `Noto Sans CJK SC` at size 24; install a suitable font on the
system or configure another with `STUDIO_SUBTITLE_FONT`. Subtitle size must be
between 8 and 96. Peak limiting and loudness normalization are not implemented;
avoid excessive combined gains. See [README-v0.6.2.md](README-v0.6.2.md).

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
