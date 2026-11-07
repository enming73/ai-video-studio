# AI Video Studio v0.4 — Reference-first foundation

This is an incremental upgrade of the supplied v0.3 project. Existing SQLite tables, video providers, VBench, paid video assets and rendering workflow are unchanged. No automatic paid image-generation or I2V API calls are implemented in this release: the existing Flux adapter is **text-to-video**, not image generation; MiniMax H3 I2V capability/payload must be independently verified before enabling paid I2V.

## Setup

```sh
cd ai-video-studio
uv sync --extra dev
uv run studio --help
```

Set `STUDIO_HOME` to the path of your existing `studio-data` directory if running from a different directory. Back up `studio.sqlite3` first. Existing rendered projects are read-only for the reference workflow.

## Reference workflow

```sh
uv run studio characters PROJECT_ID
uv run studio extract-keyframes PROJECT_ID 1 --count 3
uv run studio import-character PROJECT_ID Luma /path/to/approved-luma.png
uv run studio import-character PROJECT_ID Pico /path/to/approved-pico.png
uv run studio import-character PROJECT_ID Fern /path/to/approved-fern.png
uv run studio import-first-frame PROJECT_ID 1 /path/to/shot-01.png
uv run studio preview-frames PROJECT_ID
uv run studio check-i2v PROJECT_ID 1
```

`import-character` requires a project with a Character Bible (from `smart-plan`). Images are content-addressed and copied to `studio-data/PROJECT_ID/references/`; the manifest records SHA-256 hashes. `extract-keyframes` can be used on the original six clips for visual comparison. `check-i2v` deliberately reports `ready_for_i2v: false` and does not charge anything.

## Important limitations

- This release **does not** generate images using Flux or other image providers.
- This release **does not** send first-frame images to MiniMax or perform I2V generation.
- Reference images are stored and reviewable but **not injected** into the existing `studio generate` command (which remains text-to-video).
- The current paid video assets are preserved; there is no automatic re-generation or mutation of completed shots.
- A subsequent integration must select a documented image API and a verified MiniMax I2V-capable model with explicit cost and user opt-in, then implement a real two-shot live test.
