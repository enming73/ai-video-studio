# AI Video Studio v0.3

This archive includes `ai-video-studio/`, `video-provider-core/`, and `vbench/`.

## Install

```bash
cd ai-video-studio
uv sync --extra dev
uv run studio --help
```

## LLM storyboard (does NOT generate videos)

Set an OpenAI-compatible chat completions endpoint. The model must support `response_format: {type: json_object}`.

```bash
export STUDIO_LLM_API_KEY='...'
export STUDIO_LLM_BASE_URL='https://api.openai.com/v1'
uv run studio create --prompt 'A gray baby elephant discovers a magical jungle stream' --duration 60 --provider minimax --budget 5
uv run studio smart-plan PROJECT_ID --model YOUR_CHAT_MODEL --shot-length 10
uv run studio storyboard PROJECT_ID --output storyboard.json
uv run studio edit-shot PROJECT_ID 2 --prompt 'The same gray baby elephant follows a bright butterfly beside the jungle stream, cinematic 3D animation.'
uv run studio generate PROJECT_ID            # DRY RUN; no paid video API call
uv run studio generate PROJECT_ID --live --budget 5 --limit 1
uv run studio render PROJECT_ID
```

For free mock tests, create with `--provider mock --budget 0`, use `smart-plan` (LLM calls may still incur costs), or use `plan` for a deterministic cost-free storyboard.

## Existing projects

Do NOT run `smart-plan` on a project that already has shots; it refuses to overwrite existing paid assets. To inspect old projects, set `STUDIO_HOME` to the old `studio-data` directory before starting the CLI. Existing SQLite tables are migrated incrementally.

## Limitations

- `character_bible` is saved in SQLite and storyboard JSON; it is not yet injected into the video prompts or used to generate a reference image. LLM prompts are expected to repeat character appearance.
- LLM planning is a single paid external request and is not currently budget-metered by the video cost ceiling.
- Only PLANNED and uncharged FAILED shots may be edited. Already generated shots cannot be modified in place, to prevent losing paid assets.
- Render normalizes clips to 1280x720, 24 fps, no audio; it can pad a short source by holding the final frame. This is a v0.3 MVP, not a professional audio/video timeline.
- Provider cost rates must be set using verified environment variables; reservation amounts are estimates.
- An ambiguous remote submit must be reconciled manually with `studio attach`, not blindly retried.
- Existing v0.2 source and its shared provider library are preserved.
