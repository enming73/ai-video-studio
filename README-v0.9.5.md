# v0.9.5 Continuity Vision Agent

Adds an **opt-in** semantic boundary review for existing v0.9.4 continuity reports. Compares two frames from the end of one shot to two frames from the beginning of the next, plus both storyboard prompts. Outputs structured observations, uncertainty, severity, recommendation and an optional proposed *next-shot* prompt. Does not modify assets, submit generation jobs, or alter timeline selection.

## Browser

From `ai-video-studio/`, install dependencies with `uv sync --extra dev --extra web` and start the backend with `uv run --env-file .env studio web --port 8765`. In a second terminal run `cd ai-video-studio/web && npm install && npm run dev`, then open <http://127.0.0.1:5173>. First create a v0.9.4 continuity report, then open **Continuity Vision Agent**. Enter `2` in the pair filter to select shot 2 → 3, or use comma-separated starting shot numbers for multiple boundaries. Only ready boundaries with two frames on each side are eligible. Dry Run makes no API calls and does not save a review; the live button requires explicit confirmation.

## REST

`POST /api/projects/{pid}/director/continuity-vision`

```json
{"report_id":"<12-digit hex report ID>","model":"gpt-4o-mini","live":false,"budget_usd":0.20,"per_pair_ceiling_usd":0.20,"pairs":[2]}
```

To perform a live review, configure `OPENAI_API_KEY` in `.env` and explicitly approve the browser confirmation. The model must support image input through an OpenAI-compatible Chat Completions endpoint. Optional `OPENAI_BASE_URL` is supported (HTTPS or localhost only). Live requests send both shot prompts and the four frames to the configured provider. Direct REST clients authorize a live request by sending `live:true`; the confirmation dialog is a browser safeguard, not a separate API field.

`GET /api/projects/{pid}/director/continuity-vision` lists immutable live reviews; `GET /api/projects/{pid}/director/continuity-vision/{review_id}` returns a stored review. Reports are saved to `studio-data/<pid>/continuity-vision-reviews/<review_id>/review.json` (respecting configured core.HOME).

**Cost caveat:** per-pair ceiling is an estimate reserved before submitting, **not** a hard cap enforced by the provider. Even failed calls may incur charges. API retries are not automatic. No video regeneration is initiated.

**Evidence caveat:** four sampled frames cannot establish all motion or scene semantics. The model may miss continuity defects or hallucinate character identities. Human review remains required.

**Upgrade safety:** Never overwrite `.env`, `.venv`, `studio-data`, or `web/node_modules` from ZIP releases.
