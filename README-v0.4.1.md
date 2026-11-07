# v0.4.1 — reference image generation and isolated MiniMax-H3 I2V

Install in `ai-video-studio`: `uv sync --extra dev`.

Set `STUDIO_HOME` to your existing absolute `studio-data` directory BEFORE running commands from a different working directory. Keep existing SQLite and videos; do not replace them.

## Image generation

Image backend: OpenAI-compatible Images Edits API (`POST /images/edits`, multipart). This is **not** Flux; the existing Flux adapter is video generation. Default model: `gpt-image-2.5-sunburst` (requires image model access; Chat Completions access alone is insufficient). Set `STUDIO_IMAGE_API_KEY` or `OPENAI_API_KEY`. Optionally `STUDIO_IMAGE_BASE_URL`.

```
uv run studio frame-plan 3a5b4d58f96b 2
uv run studio generate-frame 3a5b4d58f96b 2
uv run studio generate-frame 3a5b4d58f96b 2 --live
uv run studio preview-frames 3a5b4d58f96b
```

Import character images with `studio import-character` first. `generate-frame` uses only named characters present in the shot prompt. Existing first frames are protected unless `--force`. Generated images cost money; dry run is default. Review each result visually before I2V.

## H3 I2V experiment (existing clips preserved)

MiniMax-H3 supports `first_frame` in `POST /v2/video_generation`. H3 does **not** allow first-frame and `reference_image` in the same request. Reference images influence *frame creation*, then only the approved frame is sent to I2V.

```
uv run studio generate-i2v 3a5b4d58f96b 2
uv run studio generate-i2v 3a5b4d58f96b 2 --live --max-usd 0.80
uv run studio poll-i2v 3a5b4d58f96b 2
```

The I2V experiment writes to `studio-data/PROJECT/experiments/i2v-002/` and never replaces a completed shot. `poll-i2v` downloads MP4 after success. No retry after uncertain submission without inspecting the MiniMax dashboard. Video estimate is 10s × $0.08 at 768P, excluding possible ancillary fees; check provider pricing. This release has unit tests but no paid live API verification.

**Important:** `studio generate` remains the existing T2V workflow. Do not use it to test I2V. Original `studio check-i2v` remains a conservative v0.4 check; use `generate-i2v` for the new isolated experiment.
