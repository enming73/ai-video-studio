# AI Video Studio v0.6.1 — Narration and TTS

Adds shot-level LLM narration writing, editable JSON scripts, opt-in OpenAI-compatible text-to-speech, shot-aligned SRT captions, and integration with v0.6 soundtrack/export. Existing videos and SQLite data remain unchanged. No cloud API requests happen unless `--live` is supplied.

## Install

```bash
cd ai-video-studio
uv sync --extra dev
```

Configure `.env` (pass with `uv run --env-file .env` if needed):

```dotenv
STUDIO_LLM_API_KEY=...
STUDIO_LLM_BASE_URL=https://api.openai.com/v1
STUDIO_TTS_API_KEY=...
STUDIO_TTS_BASE_URL=https://api.openai.com/v1
```

## Run on an existing project

```bash
PID=3a5b4d58f96b
uv run --env-file .env studio narration-plan "$PID" --language zh-CN --model gpt-4o-mini
uv run --env-file .env studio narration-plan "$PID" --language zh-CN --model gpt-4o-mini --live
uv run studio narration-show "$PID"
```

Edit `studio-data/$PID/narration/script.json` before TTS. The JSON contains six lines with `seq`, `duration`, `text`. Do not change seq/duration; shorten long text. The LLM plan requires an API call and is charged by the provider.

```bash
uv run --env-file .env studio narration-tts "$PID" --voice alloy --model gpt-4o-mini-tts
uv run --env-file .env studio narration-tts "$PID" --voice alloy --model gpt-4o-mini-tts --live
uv run studio narration-attach "$PID"
uv run studio export-film "$PID" --source selected --burn-subtitles
```

If you already have `final-i2v.mp4`, you can use `--video studio-data/$PID/final-i2v.mp4` instead of `--source selected`.

**Limitations:** Subtitles are aligned to shot boundaries, not to individual spoken words. TTS audio is adjusted within a limited speed range to fit the shot; if audio is more than 50% longer than the shot, the command fails and asks you to shorten the text. TTS may incur charges on successful requests even if a later step fails. An interrupted run can be restarted but may repeat calls; run artifacts are retained. The original soundtrack config is updated by `narration-attach`, preserving existing music and ambience settings. The output is a new MP4. Audio gain mixing remains basic and does not implement sidechain ducking.

## Tests

```bash
uv run --extra dev pytest -q
```
