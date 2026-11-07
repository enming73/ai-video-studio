# v0.9.3 Production Workflow Integration

React Studio includes Create → Plan → Generate, using the same core functions as the CLI. Existing projects and shots are not overwritten. No new provider credentials are stored in the browser.

## Startup

```bash
cd ai-video-studio
uv sync --extra dev --extra web
uv run --env-file .env studio web --port 8765
```

In a second terminal run `cd ai-video-studio/web && npm install && npm run dev`, then open <http://127.0.0.1:5173>. FFmpeg and FFprobe must be available on the backend machine.

Use Production at the bottom of the Studio page. Create a project, plan shots, then use **Dry Run** to inspect the cloud estimate. Paid generation requires a separate confirmation dialog, a matching project ID, and an explicit positive run budget no greater than the project budget. The provider's configured USD/sec rate must be verified; the budget is not a guaranteed billing cap. Background jobs report `queued`, `running`, `completed`, or `failed`; use **同步素材** after generation.

**Mock projects:** the Dry Run button invokes the existing local mock generation path and creates clips and updates project state; it is not an estimate-only operation. No paid API is called. Paid browser generation is rejected for mock projects.

Important: jobs are in memory and do not survive API restarts. The underlying CLI/core retains submission state in SQLite. Never automatically resubmit ambiguous `SUBMITTING` jobs; reconcile with the provider dashboard first. Do not expose the local FastAPI server publicly: it has no authentication.

## Compatibility

Existing `.env`, `.venv`, `studio-data` and media files are excluded from the release archive. The `video-provider-core` editable dependency remains a sibling package as in previous releases.
