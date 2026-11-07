# AI Video Studio + VBench shared Provider Core

This repository contains Studio, VBench, and their shared provider core. The
v0.9.4 increment adds offline evidence for reviewing shot-to-shot continuity.
Studio includes a local React/Vite visual editor backed by FastAPI, alongside
the existing CLI. See [the v0.9.4 guide](README-v0.9.4.md) for continuity
review, [the v0.9.3 guide](README-v0.9.3.md) for production,
[the v0.9.2 guide](README-v0.9.2.md) for the Director
Workspace, [the v0.9.1 guide](README-v0.9.1.md) for Director vision
review, [the v0.9.0 guide](README-v0.9.0.md) for offline Director analysis,
[the v0.8.1 guide](README-v0.8.1.md) for preview and
export, [the v0.8 guide](README-v0.8.md) for the editable
timeline, [the v0.7 guide](README-v0.7.md) for the original editor, and
[README-v0.6.2.md](README-v0.6.2.md) for the audio timeline,
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

## Director Agent: offline technical analysis

Analyze the selected video asset for each storyboard shot without paid API
calls. FFprobe records duration, resolution, codec and frame rate; FFmpeg
detects black and frozen segments and extracts review frames. The JSON report
records per-shot evidence, warnings and status. Each analysis creates a
separate report directory, preserving previous reports and source assets.

Install FFmpeg and FFprobe on the backend machine, then run:

```bash
cd ai-video-studio
uv run studio sync-assets PROJECT_ID
uv run studio director-analyze PROJECT_ID --frames 3
uv run studio director-reports PROJECT_ID
```

Reports and frames are saved under
`studio-data/PROJECT_ID/director-reports/REPORT_ID/` (or under the configured
`STUDIO_HOME`). These are technical heuristics, not semantic or aesthetic
judgments; a technical pass does not guarantee storyboard or character
consistency. See [the v0.9.0 guide](README-v0.9.0.md) for thresholds, report
statuses and API endpoints.

### Optional Director vision review

Using an existing offline report, a vision model compares ordered frames with
each shot's storyboard prompt and assesses character appearance, scene, action
evidence and visible quality. It recommends `keep`, `manual_review`, or
`regenerate_candidate`; it does not regenerate video or change selected assets.

```bash
# From ai-video-studio; REPORT_ID comes from director-reports.
uv run studio director-review PROJECT_ID REPORT_ID --model gpt-4o-mini
# Explicitly authorize paid requests after configuring OPENAI_API_KEY:
uv run --env-file .env studio director-review PROJECT_ID REPORT_ID --model gpt-4o-mini --live --budget 1.00 --per-shot-ceiling 0.15
```

The default is a dry run with no API calls. The per-shot ceiling is a
user-supplied planning reservation, not a guarantee of actual charges. Live
reviews with attempted shots are saved independently under
`studio-data/PROJECT_ID/director-reviews/REVIEW_ID/review.json` (or
`STUDIO_HOME`). Still frames cannot prove continuous action, and model results
require human review. See [the v0.9.1 guide](README-v0.9.1.md) for budget,
configuration and report details.

### Director workspace in React Studio

Start the local editor as described below, select a project, and scroll to
**Director Agent · 视觉评审工作台**. Select an existing live vision review
or click **刷新报告** after creating one with the CLI. Dry runs do not create
reviews for the workspace.

Each reviewed shot displays its source prompt, keyframes, four scores,
recommendation, findings, uncertainty and any offline warnings. Failed
reviews display an error instead of scores. Use **定位到镜头** to select the
corresponding shot in the editor. Choose **保留**, **进一步检查**,
**不采纳建议**, or **批准重生成（仅记录）**; selecting an action saves it
along with the current note. For a later note edit, click **保存备注**.

Decisions are stored per review and shot in
`studio-data/PROJECT_ID/director-reviews/REVIEW_ID/decisions.json` (or
`STUDIO_HOME`), separately from the original `review.json`. Saving a new
decision for the same shot replaces its previous decision and note. Approval
records intent only: it does not call a paid API, queue regeneration, or change
assets. The workspace reads existing reviews; it does not run vision analysis.
See [the v0.9.2 guide](README-v0.9.2.md) for its API endpoints.

### Offline shot continuity review

In the local editor, select a project with at least two storyboard shots and
selected video assets. In **Continuity Director · 镜头衔接**, click
**分析相邻镜头（免费）**. FFprobe and FFmpeg sample two frames near the end
of each shot and two near the start of the next. The workspace shows each pair
side by side with both prompts, review questions, three possible editing
strategies, and any missing-asset or extraction warnings. Use the history
selector to revisit a report or the shot button to select the next shot.

Each run saves its own JSON report and extracted frames under
`studio-data/PROJECT_ID/continuity-reports/REPORT_ID/` (or `STUDIO_HOME`).
Source videos, selections, and existing Director reviews are not changed. This
is local evidence for human review: it does not call paid AI APIs, automatically
judge continuity, or apply the suggested fixes. See
[the v0.9.4 guide](README-v0.9.4.md) for API endpoints and details.

## Local visual editor

The editor lets you browse existing projects, preview shots, select
registered original or I2V assets, and add, audition or remove sound effects
on a timeline. It reuses the existing project database. Its Production
workspace can also initiate explicitly confirmed paid video generation.

Select a purple sound-effect clip to edit it. Drag the clip to change its
start time, drag its right edge to change its duration, or adjust its timing
and gain in the left inspector. Click the timeline to position the playhead
within the corresponding shot. Edits are saved to the project's
`audio-events.json`; undo and redo cover position, duration and gain changes
and are available only for the current session.

Playback advances to the next shot automatically, and optional SFX preview
follows the playhead. Shot transitions may pause briefly. Browser SFX preview
is approximate and excludes narration, music, ambience and ducking; use the
FFmpeg export to review the final mix.

Start an FFmpeg export in the browser and follow its queued, running, completed
or failed status; when complete, open or download the film. FFmpeg must be
installed on the backend machine. Only one export runs at a time; job status
is stored in memory and is lost on server restart. Progress percentages and
cancellation are not available.

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
See [the v0.8.1 guide](README-v0.8.1.md) for details and limitations.

### Create, plan and generate in the browser

In the **Production · 创建 / 分镜 / 生成** section:

1. Enter a story, duration, provider (`mock`, `minimax`, `wan`, or `flux`)
   and project budget, then create the project.
2. Set a shot length and choose **Plan**. Existing storyboards are not
   overwritten; create a new project to plan again.
3. Set a run budget and optional shot limit (`0` means all), then select
   **Dry Run / 费用预估**. For cloud providers this shows pending shots and
   estimated reservations without a paid provider call.
4. To generate paid cloud video, check the estimate and provider rates, select
   **明确授权付费生成**, and confirm the project and budget in the dialog.
   The backend also requires explicit confirmation, the matching project ID,
   and a positive run budget no larger than the project budget.
5. The background job reports `queued`, `running`, `completed`, or `failed`.
   After completion, use **同步素材** to register generated clips.

**Mock exception:** Dry Run calls the existing mock generation path, which
creates local clips and updates project/shot state instead of only estimating;
it does not call a paid API. The paid-generation action rejects mock projects.
FFmpeg and FFprobe are required for generation and estimation. Configured
cloud prices and budget reservations are estimates, not a guarantee of the
provider's final bill. Job status is held in memory and is lost on API restart;
underlying submission state remains in SQLite. Never blindly resubmit an
ambiguous submission. See [the v0.9.3 guide](README-v0.9.3.md).

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
- No automatic retries of ambiguous submissions or parallel generation. Director scores are advisory, not automatic approval.
- Vendor APIs, model availability, and download hosts may change. Live mode requires verification.
- Do not upload `.env`, credentials, DB or generated videos.

## Version guide summary

The sections above are the consolidated starting point. Historical guides are
preserved for detailed commands and release-specific caveats; older limitations
describe their release and may have been addressed by later increments.

| Guide | Main increment |
| --- | --- |
| [v0.3](README-v0.3.md) | LLM storyboard planning, shot editing and normalized rendering; LLM planning can incur separate costs. |
| [v0.4](README-v0.4.md) | Character references, first-frame imports and keyframe inspection. |
| [v0.4.1](README-v0.4.1.md) | Opt-in reference-image generation and isolated MiniMax I2V experiment. |
| [v0.5](README-v0.5.md) | Asset registry, shot selection and non-destructive timeline rendering. |
| [v0.6](README-v0.6.md) | Local soundtrack mixing, draft subtitles and FFmpeg export. |
| [v0.6.1](README-v0.6.1.md) | Narration planning, TTS and shot-aligned subtitles. |
| [v0.6.2](README-v0.6.2.md) | Audio events, experimental ducking and subtitle font settings. |
| [v0.7](README-v0.7.md) | Local React editor for projects, assets and sound effects. |
| [v0.8](README-v0.8.md) | Draggable and resizable audio clips, inspector, seek and session undo/redo. |
| [v0.8.1](README-v0.8.1.md) | Continuous shot preview, approximate SFX audition and browser export. |
| [v0.9.0](README-v0.9.0.md) | Offline technical checks, evidence frames and historical JSON reports. |
| [v0.9.1](README-v0.9.1.md) | Opt-in vision review, planning budget and independent review reports. |
| [v0.9.2](README-v0.9.2.md) | React Director workspace and separately saved human decisions. |
| [v0.9.3](README-v0.9.3.md) | Browser project creation, planning, cost preview and explicitly confirmed background cloud generation. |
| [v0.9.4](README-v0.9.4.md) | Offline side-by-side shot-boundary evidence and human continuity review. |
