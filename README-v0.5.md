# AI Video Studio v0.5 — Asset Registry + Timeline

This release extends v0.4.1 without changing its existing SQLite `shots` records or paid assets.

## Install

```bash
cd ai-video-studio
uv sync --extra dev
```

**Important:** `STUDIO_HOME` must point to your *existing* `studio-data` folder if you installed this release in a different location. Back up `studio-data/studio.sqlite3` before first run. For example:

```bash
cp studio-data/studio.sqlite3 studio-data/studio.sqlite3.backup
```

## Import old and I2V videos

```bash
uv run studio sync-assets 3a5b4d58f96b
uv run studio assets 3a5b4d58f96b --shot 2
uv run studio timeline 3a5b4d58f96b
```

`sync-assets` discovers `shots.file` as `original` and `experiments/i2v-NNN/clip.mp4` as `i2v`. It is safe to rerun: records are deduplicated by shot/source/SHA256. On the first run, it selects I2V by default when available. It never replaces an existing timeline selection.

## Render

```bash
uv run studio render-timeline 3a5b4d58f96b --source selected
uv run studio render-timeline 3a5b4d58f96b --source i2v
uv run studio render-timeline 3a5b4d58f96b --source original
```

`selected` and `mixed` both use the per-shot timeline selection. `i2v` and `original` force that asset source for every shot and fail if a shot is missing the requested source.

To change one shot to a different version:

```bash
uv run studio assets 3a5b4d58f96b --shot 3
uv run studio select-asset 3a5b4d58f96b 3 --asset ASSET_ID
uv run studio render-timeline 3a5b4d58f96b --source mixed
```

To import additional local versions:

```bash
uv run studio import-asset 3a5b4d58f96b 3 /absolute/path/to/clip.mp4 --source manual
```

Rendering uses 1280x720, 24fps, video only (no audio); shorter clips freeze their last frame and longer clips are trimmed to the shot's planned duration. Each render creates a new `final-SOURCE-XXXXXXXX.mp4` and adjacent JSON manifest; existing output paths are never overwritten. All inputs must exist and match their registered SHA256. Render operations do not incur provider API charges.

## Tests

```bash
uv run --extra dev pytest -q
```

## Limitations

- This is a CLI timeline and asset registry, not a graphical editor.
- It does not automatically choose the best version of a shot.
- No audio or transition editing yet.
- It does not scan arbitrary new folders: use `import-asset` for custom files.
- For the current release, `render-timeline` is the new renderer; legacy `studio render` remains unchanged.
