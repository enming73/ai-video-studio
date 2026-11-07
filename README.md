# AI Video Studio v0.2 + VBench shared Provider Core

## Layout
- `video-provider-core/`: shared MiniMax, Wan, Flux implementations and secure media downloader
- `vbench/`: original benchmark, now importing shared providers through a compatibility module
- `ai-video-studio/`: Studio v0.2 CLI with cloud task persistence and mock mode

## Install
```bash
cd vbench && uv sync --extra test
cd ../ai-video-studio && uv sync --extra dev
```
Both projects use editable `../video-provider-core` uv source. Keep the sibling directories together.

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
