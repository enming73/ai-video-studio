# v0.9.0 Director Analyzer (offline foundation)

This release introduces **local technical quality analysis**, the first phase of the Director Agent. It does **not** yet perform semantic/visual LLM analysis or autonomous regeneration.

## CLI

```bash
cd ai-video-studio
uv sync --extra dev --extra web
uv run studio sync-assets PROJECT_ID
uv run studio director-analyze PROJECT_ID --frames 3
uv run studio director-reports PROJECT_ID
```

Install FFmpeg and FFprobe and make them available on `PATH`. `director-analyze` reads the selected version of each shot, uses FFprobe for video metadata, FFmpeg blackdetect/freezedetect for technical anomalies, and extracts JPEG frames per shot (three by default; `--frames` accepts 1–8). Reports and frames are written under `studio-data/PROJECT_ID/director-reports/REPORT_ID/`, or the corresponding path under `STUDIO_HOME`. Each run gets a separate report directory; existing reports, video assets and render history are not overwritten. **Zero paid API calls**.

The report warns when duration differs from the plan by more than 0.5 seconds, or resolution is below 640 pixels wide or 360 pixels high. Black detection uses a 0.25-second minimum and a 0.10 pixel threshold; freeze detection uses a 1-second minimum and a -50 dB noise tolerance. These heuristics can require human review.

## REST endpoints

- `POST /api/projects/{pid}/director/analyze?frames=3` (synchronous; may take time for long videos)
- `GET /api/projects/{pid}/director/reports`
- `GET /api/projects/{pid}/director/reports/{rid}`
- `GET /api/projects/{pid}/director/reports/{rid}/shots/{seq}/frames/{index}` (zero-based index)

The report includes selected asset IDs, evidence, warnings, and an explicit distinction between `technical_pass`, `review`, `missing`, and `error`. A `technical_pass` **does not** mean the scene matches the storyboard or that characters are consistent.

## Next increments

- v0.9.1: opt-in vision model analysis with separate budget approval, prompt/character continuity assessment, and evidence-backed recommendations.
- v0.9.2: Director workspace in React with report/frames display and human-approved regeneration.

## Packaging

Archive excludes `.venv`, `.env`, `studio-data`, `node_modules`, and `.git`. Upgrade only source files; keep your local project data and environment.
