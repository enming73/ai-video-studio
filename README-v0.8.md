# AI Video Studio v0.8.0 — Timeline Editor

Adds audio clip drag/move, resize from right edge, numeric gain/timing inspector, autosave on pointer release or after editing an inspector field, undo/redo (session-only), and a click-to-seek playhead across video shots. Audio edits persist through `PATCH /api/projects/{pid}/audio/{event_id}` and use the same `audio-events.json` consumed by FFmpeg export.

## Launch

From `ai-video-studio/`:

```sh
uv sync --extra dev --extra web
uv run studio web --port 8765
# separate terminal
cd web && npm install && npm run dev
```

Open http://127.0.0.1:5173. Use `STUDIO_HOME` to point to existing data if needed.

## Limitations

- Click-to-seek pauses the video and seeks within the corresponding shot. This is not continuous cross-shot playback.
- Audio clips have individual preview players; no live composite audio preview yet.
- Undo/redo handles audio position, duration, and gain edits only and resets on reload.
- Final render still uses `studio export-film`.
- The browser app has not been end-to-end tested against a live project in this environment.

## Packaging

The release archive excludes `.venv`, `.env`, `studio-data`, `node_modules`, build outputs, and caches. Back up your local project data before copying source files.
