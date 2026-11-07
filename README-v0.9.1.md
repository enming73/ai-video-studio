# v0.9.1 Director Vision Review

This opt-in feature reviews chronological still frames from an existing v0.9.0 offline report with an OpenAI-compatible vision chat endpoint. It never regenerates video, alters asset selection, or overwrites the original report.

First install FFmpeg and FFprobe, select the project's shot assets, and create an offline report with `director-analyze` as described in [the v0.9.0 guide](README-v0.9.0.md). Only shots with frames and a status other than `missing` or `error` are eligible for vision review.

```bash
cd ai-video-studio
uv run studio director-reports PROJECT_ID
# Dry-run: no API calls, no charges
uv run studio director-review PROJECT_ID REPORT_ID --model gpt-4o-mini
# Only after confirming model supports image input and checking provider prices:
uv run --env-file .env studio director-review PROJECT_ID REPORT_ID --model gpt-4o-mini --live --budget 1.00 --per-shot-ceiling 0.15
# Optional: limit to one shot
uv run --env-file .env studio director-review PROJECT_ID REPORT_ID --shot 3 --live --budget 0.20 --per-shot-ceiling 0.20
```

The model is asked to score character consistency, scene match, action match and visual quality from 0 to 5 (or `null` when uncertain), and return findings, uncertainty and a `keep`, `manual_review` or `regenerate_candidate` recommendation. The comparison uses the shot prompt and supplied frames; it is not a proof of character continuity across the whole film.

Uses `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` or `--base-url`. Live requests send the shot prompt and extracted frames to the configured provider. Each live review with attempted shots writes an independent JSON file under `studio-data/PROJECT_ID/director-reviews/REVIEW_ID/review.json` (or the configured `STUDIO_HOME`); dry runs do not save a review file. The `per-shot-ceiling` is a **user-defined planning reservation, not a guaranteed provider charge**. The CLI checks `shots * per-shot-ceiling <= budget` before requests; it cannot enforce provider-side actual billing, especially when requests fail or providers have different prices. Check your provider's billing limits. Review only as many shots as you authorize; repeat `--shot` to select multiple shots.

The model's judgements are subjective. A few still frames cannot prove continuous action, and no automatic regeneration is performed. If an API call fails, the attempt is recorded; earlier successful calls remain persisted.

Do not ship `.env`, `.venv`, `studio-data`, `node_modules` or API credentials in distribution archives.
