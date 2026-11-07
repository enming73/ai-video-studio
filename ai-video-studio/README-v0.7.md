# AI Video Studio v0.7 — React Timeline Editor

## Upgrade and run

Preserve the existing `studio-data` directory; use `STUDIO_HOME` if upgrading in a new checkout.

```bash
cd ai-video-studio
uv sync --extra dev --extra web
uv run studio web --port 8765
# separate terminal
cd web
npm install
npm run dev
# open http://127.0.0.1:5173
```

The web API binds only to localhost, serves registered media, and does not make paid generation requests. Do not expose the API to the Internet. `npm run build` produces a production front-end build, but v0.7 is developed/tested with the Vite dev server.

## Features
- Browse existing projects and select shots
- Preview currently selected video clip
- List/switch original and I2V assets
- View video timeline and audio event positions
- Add, listen to, and delete timed audio effects
- Reuse SQLite and all v0.6.2 CLI workflows

## CLI correction
`audio-add` now explicitly accepts `--file` and `--at` options (previous v0.6.2 used positional arguments):

```bash
uv run studio audio-add PROJECT_ID --file assets/sfx/parrot.wav --at 12.5 --duration 2.5 --gain 0.65
```

## Known limitations
- The web timeline is a first editor, not drag-and-drop; time changes are entered numerically.
- Rendering/export and paid AI generation remain CLI-only.
- Audio paths must refer to files available to the backend machine.
- No authentication; bind localhost only.
