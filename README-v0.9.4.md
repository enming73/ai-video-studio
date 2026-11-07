# v0.9.4 — Continuity Director (offline boundary review)

This version adds a **non-paid, evidence-based shot boundary viewer** to React Studio. It does not claim semantic understanding: it extracts two frames near the end of each shot and two frames near the beginning of the next, shows the corresponding storyboard prompts, and provides human review questions and possible editing strategies.

Install FFmpeg and FFprobe and make them available on `PATH`. From `ai-video-studio/`, run `uv sync --extra dev --extra web` and `uv run studio web --port 8765`. In a second terminal run `cd ai-video-studio/web && npm install && npm run dev`, then open <http://127.0.0.1:5173>. Select a project with at least two storyboard shots and selected video assets, open the Continuity Director panel and click **分析相邻镜头（免费）**. Missing assets and extraction failures appear as warnings. The history selector opens previous reports; the shot button selects the next shot in the editor.

New API endpoints:
- `POST /api/projects/{pid}/director/continuity` (offline analysis)
- `GET /api/projects/{pid}/director/continuity` (report history)
- `GET /api/projects/{pid}/director/continuity/{rid}` (report)
- `GET /api/projects/{pid}/director/continuity/{rid}/boundaries/{seq}/{side}/{index}` (evidence image)

Reports are immutable under `studio-data/<project>/continuity-reports/<report-id>/`. No original videos, timeline selections, or Director reviews are changed. **This is a boundary evidence viewer, not an automatic semantic continuity detector.** A future optional vision-model stage can compare frames and character descriptions with explicit budget authorization.
