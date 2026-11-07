# Guided storyboard and sequential production

This opt-in workflow is for newly created projects. Existing projects, history,
assets and Director reports are not migrated or deleted. The local API has no
authentication; keep it on loopback and never expose it publicly.

In React Studio, create a project with **引导式逐镜制作** selected. Choose a
provider and total duration. In the guided section, enter the LLM model, run
**Dry Run**, check the reported allowable shot lengths and approve a separate
positive budget to generate a storyboard. The story text is sent to the
configured LLM. There is no provider-independent price estimate: unknown cost
is shown as unknown. A user-entered budget is an authorization/reservation,
not a vendor-enforced limit; actual charges may be higher, including on failure.
The existing LLM client reads `STUDIO_LLM_API_KEY` and optionally
`STUDIO_LLM_BASE_URL`; keep credentials in the local environment or `.env` and
do not copy them into upgrades. Cloud video pricing uses the appropriate
`MINIMAX_USD_PER_SECOND`, `WAN_USD_PER_SECOND`, or `FAL_USD_PER_SECOND` setting.

Edit each shot's description and integer duration directly in its card; add or
delete cards as needed. Save the draft and confirm the whole storyboard after
the sum equals the project duration and every shot obeys provider limits. Then
generate each shot in order. Cloud generation requires a verified USD-per-second
environment setting and a separate confirmation and budget per attempt; Mock
is local and free. Select a completed asset, preview it and confirm the shot
before moving to the next one. Previous versions remain registered; each attempt
writes to a distinct path. Return to an approved shot to edit or choose another
asset; it becomes unconfirmed, but later approvals remain unchanged. Render is
allowed only when all shots are confirmed. Without a soundtrack, guided render
exports a silent film.

Generation attempts and approvals are stored in SQLite. If submission is
uncertain, do not resubmit: check the provider dashboard, attach the verified
remote task ID and use the recovery button. Polling a known task is safe; task
results and provider links may expire. Uncertain LLM requests also block a new
request until the user acknowledges having checked provider charges. This does
not mean the original request was free. Render job status remains in memory
and is lost on API restart; source assets and completed outputs remain on disk.

Duration constraints are limited to the current text-to-video payloads; see
[provider evidence](research/2026-11-08-provider-durations.md). Provider
availability, pricing and limits may change; validate them before paid use.

CLI `smart-plan` defaults to Dry Run. Without `--shot-length`, the LLM allocates
variable shot durations; with the option, shots use that duration except for a
possibly shorter final shot, which must also be supported. `--live --budget AMOUNT` explicitly authorizes a
paid request and writes a validated storyboard only if no existing shots exist.
`generate-frame`, `narration-plan`, and `narration-tts` similarly require both
`--live` and `--budget AMOUNT`; I2V uses `--live --max-usd AMOUNT`. The
programmatic legacy `core.smart_plan` now requires `budget` and `confirm_paid`
keyword arguments. Never put API keys, `.env`, `.venv`, `studio-data`,
`node_modules`, or `.git` into a release archive.
