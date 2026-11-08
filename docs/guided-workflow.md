# Guided storyboard and sequential production

This opt-in workflow is for newly created projects. Existing projects, history,
assets and Director reports are not migrated or deleted. The local API has no
authentication; keep it on loopback and never expose it publicly.

**New guided projects start with the character/first-frame stage enabled.**
Projects created before this change keep their previous flow and show an explicit
**启用角色定妆** button; nothing is migrated or deleted until the user activates
it. Activation preserves the storyboard, selected assets, videos and Director
reports, and marks existing footage as having unknown first-frame/character
provenance. That footage cannot be rendered under the character workflow until
the user explicitly keeps it.

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

## Character styling

The LLM proposes character names, descriptions and each shot's cast. Review the
cast on every shot card; the system stores explicit character IDs rather than
guessing from prompt substrings. For each character:

1. Edit the name and description.
2. Upload a PNG/JPEG/WebP (maximum 20 MiB) or generate a candidate through the
   configured OpenAI-compatible Images API.
3. Choose one image as the primary reference and confirm the character. Every
   upload and generation creates a new immutable version; old images remain.
4. If the project has no characters, confirm that explicitly.

New character-image generation and first-frame generation each require a fresh
Dry Run, positive budget and explicit confirmation. The Images API price is not
estimated by this project: treat the amount as an authorization limit, not a
provider-enforced cap. `STUDIO_IMAGE_API_KEY` (or `OPENAI_API_KEY`) and an HTTPS
`STUDIO_IMAGE_BASE_URL` are required. Uploaded images are validated and stored
under the project's `guided-images` directory; the browser cannot submit server
paths. The project uses `/images/generations` for character portraits and
`/images/edits` for first frames with confirmed character references.

All characters and the full storyboard must be confirmed before first-frame
generation. Each shot then uses its confirmed cast; a shot with no cast uses
text-to-image. A generated first frame is only a candidate: preview and confirm
it before creating video. Changing a shot's description, cast or character
reference marks affected frames stale and invalidates only that shot's frame
confirmation.

## First frame to video

Character-enabled projects require a confirmed first frame **and** an explicitly
selected image-to-video model before video generation. The current verified
option is `MiniMax-H3` (`image_url` with role `first_frame`, 4–15 second integer
durations); it requires `MINIMAX_API_KEY`. Its documented frame limits are
checked before submission. Mock remains available for local tests. Flux 3 Max
image-to-video supports 5–20 second integer durations and first and last frames;
it requires `FAL_KEY` and a configured `FAL_USD_PER_SECOND`. Input frames are
uploaded to fal storage, where file URLs are publicly accessible by default.
Account access, pricing, and live generation must be verified separately.
An unsupported/unverified model is blocked; the project never silently falls
back to text-to-video.

Changing a character's confirmed image invalidates that character and only the
shots that use it. Existing first frames and videos are preserved and marked
with their version lineage. An affected old video is kept, but must be
explicitly retained or replaced before render. Videos with unknown provenance
are labelled as such, never presented as evidence of a newer first frame or
character design.

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
results and provider links may expire. Uncertain LLM or image requests also
block a new request until the user acknowledges having checked provider charges.
This does not mean the original request was free. Render job status remains in
memory and is lost on API restart; source assets and completed outputs remain on
disk.

Duration and I2V constraints are limited to documented text-to-video and
first-frame payloads; see [provider evidence](research/2026-11-08-provider-durations.md)
and [image/I2V evidence](research/2026-11-08-image-i2v-capabilities.md). Provider
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
