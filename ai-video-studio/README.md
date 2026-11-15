
## Persistent generation API

Legacy cloud generation now creates persistent jobs. The API exposes
`GET /api/projects/{PID}/jobs`, `GET /api/jobs/{JOB_ID}`,
`POST /api/jobs/{JOB_ID}/pause|resume|cancel` and
`GET /api/projects/{PID}/costs`. `POST /api/projects/{PID}/production/generate`
still requires explicit paid confirmation and a run budget; Dry Run never creates
jobs. `studio web` starts an embedded worker unless
`STUDIO_DISABLE_EMBEDDED_WORKER=1`. Job state and costs survive API restarts.
