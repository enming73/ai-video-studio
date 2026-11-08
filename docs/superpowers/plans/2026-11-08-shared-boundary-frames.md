# Shared Boundary Frames Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-shot first frames with N+1 shared boundary frames, allow shots to be produced in any order, and make storyboard structure edits atomic and non-destructive.

**Architecture:** Add stable `guided_boundaries` rows plus immutable candidate versions. A shot uses boundary `seq-1` and `seq`. Video generation submits both as MiniMax `first_frame` and `last_frame`. All invalidations mark old media stale and require an explicit keep/redo decision; files are never deleted. A single `PUT /structure` transaction reindexes shots and rebuilds boundary identities by adjacency.

**Tech Stack:** Python 3.11+, SQLite, FastAPI/Pydantic, React/Vite, FFmpeg/FFprobe, httpx, existing OpenAI-compatible image service and MiniMax provider.

**Spec:** `docs/superpowers/specs/2026-11-08-shared-boundary-frames-design.md`

## Global Constraints

- Never delete or overwrite user media, `shot_assets`, attempt rows, confirmation history, or `studio-data`. Archive means detach from the active sequence, not delete.
- No paid API call without an explicit positive budget and confirmation; previews are Dry Run. Reservations are not provider-enforced caps.
- Persist request IDs and uncertain states before paid calls; never auto-retry an ambiguous submission.
- Server-side gates are authoritative; frontend disabling is only a convenience.
- Unknown provider capability means blocked, never silently substituted.
- Non-guided projects, the CLI, and the legacy timeline must keep working.
- Local commits are allowed only if the user authorized them; never push or merge. If commits are not authorized, skip the commit step and report.
- Tests must not call paid services. Use `mock` providers and local FFmpeg.

## Review Focus

- Deleting a middle shot merges two boundaries but keeps both old candidates unconfirmed, and marks both neighbors' videos stale.
- A paid image request in flight must block structure edits so its result cannot attach to an archived or merged boundary.
- Changing one boundary must preserve both adjacent old videos for an explicit keep/redo decision; rendering stays blocked until decided.
- Editing a shot prompt or cast invalidates both adjacent boundaries, not only that shot's own frames.
- Opening a legacy guided project must be idempotent and lossless, and a 3:2 candidate cannot be confirmed before an explicit crop.

---

### Task 1: Boundary persistence and derived prompts

**Files:**
- Modify: `ai-video-studio/studio/core.py` (schema block near the `guided_frame_*` tables; `ALTER TABLE` guards)
- Create: `ai-video-studio/studio/guided_boundaries.py`
- Modify: `ai-video-studio/studio/guided_workflow.py:96-132` (`confirm_storyboard`), `get` at `:31-64`
- Modify: `ai-video-studio/tests/conftest.py` (shared fixtures)
- Test: `ai-video-studio/tests/test_guided_boundaries.py`

**Interfaces:**
- Consumes: `core.connect`, `guided_workflow._row`, `guided_characters._enabled`.
- Produces schema: the four boundary tables, `shots.archived`, and `guided_video_provenance.last_frame_id/last_frame_sha256` (added now so later tasks and tests can use them).
- Produces:
  - `ensure_for_storyboard(db, pid: str) -> None`
  - `list_state(db, pid: str) -> list[dict]` with keys `id, position, confirmed_image_id, confirmed, candidate_count, confirmed_stale`
  - `for_shot(db, pid: str, shot_id: str) -> tuple[dict, dict]`
  - `derive(db, pid: str, boundary_id: str) -> dict` with keys `boundary_id, position, prompt, references`; each reference is `{id, name, description, image_id, sha256}`
  - `ready(db, pid: str, boundary_id: str) -> bool`
  - fixture `boundary_project` in `tests/conftest.py` returns `(pid, state)` for a confirmed 2-shot character-stage project.
  - `workflow.get()` gains per-shot `start_boundary_id` / `end_boundary_id` and top-level `boundaries` (`None` for non-character projects).

- [ ] **Step 1: Write the failing tests**

```python
# tests/conftest.py (append; add guided_characters to the existing studio import)
def boundary_draft():
    return {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks through the forest.', 'duration': 8, 'character_ids': []},
        {'prompt': 'An elephant reaches the river.', 'duration': 12, 'character_ids': []}]}


@pytest.fixture
def boundary_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = guided_characters.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, boundary_draft(), state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    return pid, state
```

```python
# tests/test_guided_boundaries.py
from studio import core, guided_boundaries as boundaries, guided_workflow as workflow


def test_confirmed_storyboard_creates_n_plus_one_boundaries(boundary_project):
    pid, state = boundary_project
    assert [b['position'] for b in state['boundaries']] == [0, 1, 2]
    assert all(not b['confirmed'] for b in state['boundaries'])
    shots = state['shots']
    assert shots[0]['start_boundary_id'] == state['boundaries'][0]['id']
    assert shots[0]['end_boundary_id'] == state['boundaries'][1]['id']
    assert shots[1]['start_boundary_id'] == state['boundaries'][1]['id']


def test_derived_prompts(boundary_project):
    pid, state = boundary_project
    first, middle, last = state['boundaries']
    with core.connect() as db:
        assert boundaries.derive(db, pid, first['id'])['prompt'] == (
            'Create a cinematic opening frame for shot 1: An elephant walks through the forest.')
        assert boundaries.derive(db, pid, middle['id'])['prompt'] == (
            'Create a cinematic transition frame. End of shot 1: An elephant walks through the forest. '
            'Next: An elephant reaches the river.')
        assert boundaries.derive(db, pid, last['id'])['prompt'] == (
            'Create a cinematic closing frame for shot 2: An elephant reaches the river.')


def test_non_character_project_has_no_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Plain project', 10, 'mock', 0)
    workflow.create(pid)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'A river flows in the valley.', 'duration': 10}]}, 0)
    state = workflow.confirm_storyboard(pid, state['revision'])
    assert state['boundaries'] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_boundaries.py -v`
Expected: FAIL with `ModuleNotFoundError: studio.guided_boundaries` or `KeyError: 'boundaries'`.

- [ ] **Step 3: Add schema and implement**

Add the four tables from the spec (`guided_boundaries`, `guided_boundary_versions`, `guided_boundary_details`, `guided_boundary_characters`) to the `core.connect()` migration block, plus `ALTER TABLE shots ADD COLUMN archived INTEGER NOT NULL DEFAULT 0` guarded by `PRAGMA table_info(shots)`.

Implement `derive` by loading active shots ordered by `seq`, locating the boundary position, and applying the three prompt forms verbatim from the spec. References: union of `guided_shot_characters` for the adjacent shots, joined to confirmed character images, deduplicated and ordered by `character_id`.

`ensure_for_storyboard` inserts positions `0..N` inside the caller's transaction. Call it at the end of `confirm_storyboard` only when a `guided_character_settings` row exists.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_boundaries.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/core.py ai-video-studio/studio/guided_boundaries.py \
  ai-video-studio/studio/guided_workflow.py ai-video-studio/tests/test_guided_boundaries.py
git commit -m "feat: persist shared boundary frames"
```

---

### Task 2: Boundary frame generation, 16:9 normalization, confirm and crop

**Files:**
- Modify: `ai-video-studio/studio/guided_images.py` (keep character functions; leave `preview_frame`, `generate_frame`, `confirm_frame`, `list_frames` in place until Task 10 removes them with their tests)
- Modify: `ai-video-studio/studio/guided_boundaries.py`
- Create: `ai-video-studio/tests/helpers.py` (`make_png`, `attach_video`, `add_version`)
- Test: `ai-video-studio/tests/test_guided_boundary_images.py`

**Interfaces:**
- Consumes: Task 1 `derive`, `for_shot`, `boundary_project`; `guided_images._safe_path`, `_image_request`, `_image_edit`, `image_dimensions`, `image_path`.
- Produces:
  - `guided_images.crop_to_16x9(source: Path, output: Path) -> bool` (crops with FFmpeg and returns whether a crop happened)
  - `boundaries.insert_version(db, pid, boundary_id, file: Path, *, stale: bool, prompt: str, model: str, references: list[tuple[str, str]]) -> str` (shared by generation, crop and tests)
  - `boundaries.preview(pid, boundary_id, model) -> dict` with `boundary_id, position, model, prompt, references, estimated_cost_usd, note`
  - `boundaries.generate(pid, boundary_id, model, budget_usd, *, confirm_paid, request_id, revision) -> dict` (attempt row)
  - `boundaries.list_frames(pid, boundary_id) -> list[dict]`
  - `boundaries.confirm(pid, boundary_id, image_id, revision, *, allow_stale=False) -> dict` with `confirmed_image_id, revision, affected_shot_ids`
  - `boundaries.unconfirm(pid, boundary_id, revision) -> dict`
  - `boundaries.crop(pid, boundary_id, image_id, revision) -> dict` with `id, revision`
  - `mark_affected(db, pid, boundary_id) -> list[str]`
  - `shot_ready(db, pid, shot_id) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/helpers.py
import subprocess
from pathlib import Path

from studio import core, guided_boundaries as boundaries, timeline


def make_png(path, size='1536x1024'):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=red:s={size}',
                    '-frames:v', '1', str(path)], check=True)
    return path.read_bytes()


def attach_video(pid, shot, first_id, last_id):
    path = Path(core.HOME) / f'video-{shot["seq"]}.mp4'
    path.write_bytes(b'old-video')
    asset = timeline.import_asset(pid, shot['seq'], str(path))
    timeline.select(pid, shot['seq'], asset)
    with core.connect() as db:
        db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status,asset_id) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ('a-' + shot['id'], pid, shot['id'], 'r-' + shot['id'], shot['prompt'], shot['duration'],
                    'mock', 0, 'COMPLETED', asset))
        db.execute("INSERT INTO guided_video_provenance(attempt_id,frame_id,frame_sha256,last_frame_id,last_frame_sha256,model) "
                   "VALUES (?,?,?,?,?,?)",
                   ('a-' + shot['id'], first_id, 'f', last_id, 'l', 'mock'))
    return path


def add_version(pid, boundary_id, name, size='1536x1024', stale=False):
    directory = Path(core.HOME) / pid / 'guided-images'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{name}.png'
    make_png(path, size)
    with core.connect() as db:
        return boundaries.insert_version(db, pid, boundary_id, path, stale=stale,
                                         prompt='p', model='test', references=[])
```

```python
# tests/test_guided_boundary_images.py
import base64
from pathlib import Path

from studio import core, guided_boundaries as boundaries, guided_images as images, guided_workflow as workflow
from tests.helpers import add_version, attach_video, make_png


def test_preview_does_not_require_earlier_shots(boundary_project):
    pid, state = boundary_project
    details = boundaries.preview(pid, state['shots'][1]['start_boundary_id'], 'gpt-image-2.5-sunburst')
    assert details['position'] == 1
    assert 'End of shot 1' in details['prompt']


def test_generated_frame_is_cropped_to_16_9(boundary_project, monkeypatch):
    pid, state = boundary_project
    raw = make_png(Path(core.HOME) / 'raw.png')
    monkeypatch.setenv('STUDIO_IMAGE_API_KEY', 'fake')
    encoded = {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
    monkeypatch.setattr(images, '_image_request', lambda *a, **k: encoded)
    monkeypatch.setattr(images, '_image_edit', lambda *a, **k: encoded)
    attempt = boundaries.generate(pid, state['boundaries'][0]['id'], 'gpt-image-2.5-sunburst', 1,
                                  confirm_paid=True, request_id='frame-1', revision=state['revision'])
    assert attempt['status'] == 'COMPLETED'
    assert images.image_dimensions(images.image_path(pid, attempt['image_id'])) == (1536, 864)


def test_confirm_and_unconfirm_cascade_to_both_neighbours(boundary_project):
    pid, state = boundary_project
    first, middle, last = state['boundaries']
    shot_a, shot_b = state['shots']
    asset_a = attach_video(pid, shot_a, first['id'], middle['id'])
    asset_b = attach_video(pid, shot_b, middle['id'], last['id'])
    new_id = add_version(pid, middle['id'], 'middle')
    current = workflow.get(pid)['revision']
    boundaries.confirm(pid, middle['id'], new_id, current)
    state = workflow.get(pid)
    assert [s['old_video_decision_required'] for s in state['shots']] == [True, True]
    assert asset_a.exists() and asset_b.exists()
    boundaries.unconfirm(pid, middle['id'], state['revision'])
    assert [s['old_video_decision_required'] for s in workflow.get(pid)['shots']] == [True, True]


def test_crop_creates_a_new_version_and_keeps_the_old(boundary_project):
    pid, state = boundary_project
    boundary_id = state['boundaries'][0]['id']
    original = add_version(pid, boundary_id, 'legacy-3x2')
    original_bytes = images.image_path(pid, original).read_bytes()
    result = boundaries.crop(pid, boundary_id, original, state['revision'])
    assert result['id'] != original
    assert images.image_dimensions(images.image_path(pid, result['id'])) == (1536, 864)
    assert images.image_path(pid, original).read_bytes() == original_bytes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_boundary_images.py -v`
Expected: FAIL with missing functions.

- [ ] **Step 3: Implement**

- `crop_to_16x9`: probe with `image_dimensions`; if `abs(w/h - 16/9) <= 0.01`, copy the source to `output` and return `False`. Otherwise compute the largest centered 16:9 rectangle, run `ffmpeg -v error -y -i IN -vf crop=W:H:X:Y,scale=1536:864 -frames:v 1 OUT.png`, verify it with the existing `-f null -` check, and return `True`.
- `insert_version`: verify the file is inside the project image directory, hash it, insert the version/detail/reference rows, and return the new ID.
- `crop`: create a new version from the cropped file and preserve the source's `stale` flag; cropping normalizes aspect ratio but does not make an outdated prompt current.
- `generate`: mirror the old `generate_frame` flow but target `boundary_id` and `kind='boundary'`; request `1536x1024`, decode, write a temp raw file, crop it into `_safe_path(pid, image_id, '.png')`, hash the cropped bytes, then insert via `insert_version`.
- `confirm`: reject a missing/stale version unless `allow_stale`; reject non-16:9 dimensions; write `confirmed_image_id`, bump `guided_projects.revision`; call `mark_affected`.
- `unconfirm`: clear `confirmed_image_id`, bump revision, then `mark_affected`.
- `crop` and `generate` bump revision after inserting a version.
- `mark_affected`: boundary at position `p` affects 0-based shot indices `p-1` and `p`. For each affected shot with a `timeline_items` row, read its asset's provenance; if it is absent or its `frame_id`/`last_frame_id` differ from the current confirmed boundary pair, upsert `guided_stale_shots` with `keep_asset_id=NULL` and delete `guided_confirmations`.
- `shot_ready`: both `for_shot` boundaries satisfy `ready`.
- `image_path`: extend the UNION with `guided_boundary_versions`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_boundary_images.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/guided_images.py ai-video-studio/studio/guided_boundaries.py \
  ai-video-studio/tests/test_guided_boundary_images.py ai-video-studio/tests/conftest.py
git commit -m "feat: generate and confirm 16:9 boundary frames"
```

---

### Task 3: Cascade invalidation for prompt, cast and character changes

**Files:**
- Modify: `ai-video-studio/studio/guided_boundaries.py`
- Modify: `ai-video-studio/studio/guided_characters.py:94-105` (`_invalidate_character`, `invalidate_shot`), `set_shot_characters` at `:200-220`
- Modify: `ai-video-studio/studio/guided_workflow.py:159-177` (`edit_shot`)
- Test: `ai-video-studio/tests/test_guided_cascade.py`

**Interfaces:**
- Consumes: Task 2 `mark_affected`, `shot_ready`, `insert_version`; Task 1 `for_shot`; tests helpers.
- Produces:
  - `invalidate_for_shot(db, pid, shot_id) -> None`
  - `invalidate_for_character(db, pid, character_id) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guided_cascade.py
import pytest

from studio import core, guided_boundaries as boundaries, guided_characters as roles
from studio import guided_images as images, guided_workflow as workflow
from tests.helpers import add_version, attach_video, make_png


@pytest.fixture
def three(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant drinks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant sleeps.', 'duration': 8, 'character_ids': []}]}, state['revision'])
    return pid, workflow.confirm_storyboard(pid, state['revision'])


def confirm_all(pid, state):
    for i, boundary in enumerate(state['boundaries']):
        version = add_version(pid, boundary['id'], f'version-{i}')
        boundaries.confirm(pid, boundary['id'], version, workflow.get(pid)['revision'])
    return workflow.get(pid)


def test_boundary_change_marks_both_neighbours_but_not_others(three):
    pid, state = three
    b, shots = state['boundaries'], state['shots']
    attach_video(pid, shots[0], b[0]['id'], b[1]['id'])
    attach_video(pid, shots[1], b[1]['id'], b[2]['id'])
    attach_video(pid, shots[2], b[2]['id'], b[3]['id'])
    new_id = add_version(pid, b[1]['id'], 'new-b1')
    boundaries.confirm(pid, b[1]['id'], new_id, workflow.get(pid)['revision'])
    assert [s['old_video_decision_required'] for s in workflow.get(pid)['shots']] == [True, True, False]


def test_prompt_edit_invalidates_both_adjacent_boundaries(three):
    pid, state = three
    confirm_all(pid, state)
    workflow.edit_shot(pid, 2, 'An elephant drinks slowly.', workflow.get(pid)['revision'])
    state = workflow.get(pid)
    assert [b['confirmed'] for b in state['boundaries']] == [True, False, False, True]


def test_cast_edit_invalidates_internal_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    cid = state['characters'][0]['id']
    raw = make_png(tmp_path / 'luma.png', '512x512')
    image = images.import_character_image(pid, cid, 'luma.png', raw, state['revision'])
    state = roles.confirm_character(pid, cid, image['id'], image['revision'])
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'Luma walks.', 'duration': 5, 'character_ids': [cid]},
        {'prompt': 'Luma drinks.', 'duration': 5, 'character_ids': [cid]}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    confirm_all(pid, state)
    roles.set_shot_characters(pid, 1, [], workflow.get(pid)['revision'])
    assert workflow.get(pid)['boundaries'][1]['confirmed'] is False


def test_character_image_change_only_affects_boundaries_that_used_it(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 10, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.save_character(pid, None, 'Luma', 'Small gray elephant', 0)
    cid = state['characters'][0]['id']
    first = images.import_character_image(pid, cid, 'luma.png', make_png(tmp_path / 'luma.png', '512x512'), state['revision'])
    state = roles.confirm_character(pid, cid, first['id'], first['revision'])
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'Luma walks.', 'duration': 5, 'character_ids': [cid]},
        {'prompt': 'The river is quiet.', 'duration': 5, 'character_ids': []}]}, state['revision'])
    state = workflow.confirm_storyboard(pid, state['revision'])
    confirm_all(pid, state)
    second = images.import_character_image(pid, cid, 'luma2.png', make_png(tmp_path / 'luma2.png', '512x512'),
                                           workflow.get(pid)['revision'])
    roles.confirm_character(pid, cid, second['id'], second['revision'])
    assert [b['confirmed'] for b in workflow.get(pid)['boundaries']] == [False, False, True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_cascade.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`invalidate_for_shot` finds the shot's two boundaries, sets `stale=1` on all their versions, clears both `confirmed_image_id` values, then calls `mark_affected` for each. `guided_characters.invalidate_shot` delegates to it when boundaries exist, otherwise keeps the legacy frame behavior. `guided_characters.set_shot_characters` calls `invalidate_for_shot` when the cast set changes, before the per-shot legacy invalidation.

`invalidate_for_character` finds boundary versions linked through `guided_boundary_characters`, marks those versions stale, clears the hosting boundaries' confirmations, calls `mark_affected`, and keeps the existing per-shot cast invalidation for videos.

`guided_workflow.edit_shot` keeps the current prompt-update and confirmation deletion, and additionally calls `boundaries.invalidate_for_shot`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_cascade.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/guided_boundaries.py ai-video-studio/studio/guided_characters.py \
  ai-video-studio/studio/guided_workflow.py ai-video-studio/tests/test_guided_cascade.py
git commit -m "feat: cascade boundary changes to adjacent shots"
```

---

### Task 4: MiniMax first-and-last-frame payload

**Files:**
- Modify: `video-provider-core/src/video_provider_core/providers.py:55-65`
- Modify: `ai-video-studio/studio/provider_capabilities.py:10-19`
- Test: `ai-video-studio/tests/test_guided_i2v.py`

**Interfaces:**
- Produces: `Provider.submit_i2v(prompt, duration, first_frame, mime_type, last_frame=None, last_mime_type=None) -> str`
- Produces: `i2v_capabilities(...)['first_last_frame'] -> bool`

- [ ] **Step 1: Write the failing tests**

```python
def test_minimax_i2v_payload_contains_first_and_last_frame(monkeypatch):
    # handler captures JSON; call submit_i2v('A forest',5,b'first','image/png',b'last','image/png')
    # assert content[1]['role'] == 'first_frame', content[2]['role'] == 'last_frame'
    # assert both decoded bodies match exactly.

def test_last_frame_requires_first_frame_and_supported_type():
    # submit_i2v(..., first_frame=b'', ...) and last_frame with mime 'image/gif' both raise ValueError
    # without an HTTP call.

def test_minimax_capability_declares_first_last_frame():
    assert provider_capabilities.i2v_capabilities('minimax', 'MiniMax-H3')['first_last_frame'] is True
    assert provider_capabilities.i2v_capabilities('flux', 'x')['verified'] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_i2v.py -v`
Expected: FAIL with unexpected keyword or missing key.

- [ ] **Step 3: Implement**

Validate both images and both MIME types before building the payload. Keep `ratio='adaptive'`. Add `first_last_frame` to each `_I2V` entry (`True` for `mock` and `minimax`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_i2v.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add video-provider-core/src/video_provider_core/providers.py \
  ai-video-studio/studio/provider_capabilities.py ai-video-studio/tests/test_guided_i2v.py
git commit -m "feat: submit first and last frames to MiniMax"
```

---

### Task 5: Video generation from two boundaries in any order

**Files:**
- Modify: `ai-video-studio/studio/guided_generation.py:18-30`, `:46-78`, `:104-118`, `:120-186`
- Modify: `ai-video-studio/studio/guided_workflow.py:135-156` (`confirm_shot`)
- Test: `ai-video-studio/tests/test_guided_generation.py`

**Interfaces:**
- Consumes: Tasks 1-4, including the provenance columns added in Task 1.
- Produces: `preview` keys `first_frame_id, first_frame_sha256, last_frame_id, last_frame_sha256`; attempts store both frame IDs in provenance.

- [ ] **Step 1: Write the failing tests**

Replace `test_preview_and_order_rejection_do_not_create_attempts` with:

```python
import pytest

from studio import core, guided_boundaries as boundaries, guided_generation as generation, guided_workflow as workflow, timeline
from tests.helpers import add_version


@pytest.fixture
def video_project(boundary_project, monkeypatch):
    pid, state = boundary_project
    generation.set_video_model(pid, 'mock', 'mock', workflow.get(pid)['revision'])
    monkeypatch.setattr(generation, '_mock_frame_video',
                        lambda shot, target, *args, **kwargs: target.write_bytes(b'generated'))
    monkeypatch.setattr(core, 'verify_video', lambda path, duration: None)
    return pid, workflow.get(pid)


def confirm_pair(pid, state, *positions):
    for position in positions:
        boundary = state['boundaries'][position]
        version = add_version(pid, boundary['id'], f'b{position}')
        boundaries.confirm(pid, boundary['id'], version, workflow.get(pid)['revision'])
    return workflow.get(pid)


def test_any_shot_with_both_boundaries_can_generate_first(video_project):
    pid, state = video_project
    state = confirm_pair(pid, state, 1, 2)
    assert generation.preview(pid, 2)['last_frame_id'] == state['boundaries'][2]['confirmed_image_id']
    attempt = generation.submit(pid, 2, 1, confirm_paid=True, request_id='second')
    assert attempt['status'] == 'COMPLETED'
    assert timeline.assets(pid, 1) == []


def test_missing_boundary_blocks_generation(video_project):
    pid, _ = video_project
    with pytest.raises(ValueError, match='boundary'):
        generation.preview(pid, 1)


def test_provenance_records_both_boundaries(video_project):
    pid, state = video_project
    state = confirm_pair(pid, state, 0, 1)
    attempt = generation.submit(pid, 1, 1, confirm_paid=True, request_id='first')
    with core.connect() as db:
        row = db.execute('SELECT frame_id,last_frame_id FROM guided_video_provenance WHERE attempt_id=?',
                         (attempt['id'],)).fetchone()
    assert (row['frame_id'], row['last_frame_id']) == (
        state['boundaries'][0]['confirmed_image_id'], state['boundaries'][1]['confirmed_image_id'])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_generation.py -v`
Expected: FAIL with the order error or missing provenance columns.

- [ ] **Step 3: Implement**

- Remove the `missing` earlier-shot query from `_shot`.
- In `preview`, replace the single frame lookup with `boundaries.for_shot` plus `boundaries.ready`; raise `ValueError('Confirm both boundary frames before video generation')` when either is missing.
- Read both images in `submit`, verify both hashes, and pass both to `submit_i2v`.
- `_mock_frame_video(shot, target, first_frame, first_mime, last_frame, last_mime)` writes both inputs and produces a deterministic two-half clip of the exact duration (first frame for the first half, last frame for the second).
- Insert provenance with both frame IDs.
- Remove the earlier-shot check from `confirm_shot`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_generation.py tests/test_guided_i2v.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/core.py ai-video-studio/studio/guided_generation.py \
  ai-video-studio/studio/guided_workflow.py ai-video-studio/tests/test_guided_generation.py
git commit -m "feat: generate any shot from its two confirmed boundaries"
```

---

### Task 6: Atomic storyboard structure revision

**Files:**
- Create: `ai-video-studio/studio/guided_structure.py`
- Modify: `ai-video-studio/studio/timeline.py:38-40` (active shots), `timeline()` and `assets()` (include `archived`)
- Modify: `ai-video-studio/studio/guided_workflow.py` (`get` must still work with archived rows)
- Test: `ai-video-studio/tests/test_guided_structure.py`

**Interfaces:**
- Consumes: `provider_capabilities`, `timeline`, `guided_boundaries`.
- Produces: `apply(pid: str, shots: list[dict], revision: int) -> dict`; each input shot is `{id: str|None, prompt: str, duration: int, character_ids: list[str]}` and the return is `workflow.get(pid)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guided_structure.py
import pytest

from studio import core, guided_boundaries as boundaries, guided_characters as roles
from studio import guided_structure, guided_workflow as workflow, timeline
from tests.helpers import add_version, attach_video


@pytest.fixture
def structure_project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'HOME', tmp_path)
    pid = core.create('Elephant in forest', 20, 'mock', 0)
    workflow.create(pid, character_stage_enabled=True)
    state = roles.confirm_empty_cast(pid, 0)
    state = workflow.save_draft(pid, {'character_bible': {}, 'shots': [
        {'prompt': 'An elephant walks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant drinks.', 'duration': 6, 'character_ids': []},
        {'prompt': 'An elephant sleeps.', 'duration': 8, 'character_ids': []}]}, state['revision'])
    return pid, workflow.confirm_storyboard(pid, state['revision'])


def target(state, durations):
    return [{'id': s['id'], 'prompt': s['prompt'], 'duration': d, 'character_ids': []}
            for s, d in zip(state['shots'], durations)]


def confirm(pid, position, state, name):
    version = add_version(pid, state['boundaries'][position]['id'], name)
    return boundaries.confirm(pid, state['boundaries'][position]['id'], version,
                              workflow.get(pid)['revision'])


def test_total_duration_must_match(structure_project):
    pid, state = structure_project
    with pytest.raises(ValueError, match='sum'):
        guided_structure.apply(pid, target(state, [7, 6, 8]), state['revision'])


def test_confirmed_shot_duration_cannot_change_directly(structure_project):
    pid, state = structure_project
    attach_video(pid, state['shots'][0], 'a', 'b')
    with pytest.raises(ValueError, match='archive'):
        guided_structure.apply(pid, target(state, [8, 6, 6]), workflow.get(pid)['revision'])


def test_archive_preserves_asset_and_compacts_sequence(structure_project):
    pid, state = structure_project
    path = attach_video(pid, state['shots'][0], 'a', 'b')
    guided_structure.apply(pid, target(state, [6, 10, 10])[1:], workflow.get(pid)['revision'])
    assert path.exists()
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM shot_assets WHERE file=?', (str(path),)).fetchone()[0] == 1
        assert [r['seq'] for r in db.execute('SELECT seq FROM shots WHERE project_id=? AND archived=0 ORDER BY seq', (pid,))] == [1, 2]
        assert db.execute('SELECT seq FROM shots WHERE project_id=? AND archived=1', (pid,)).fetchone()['seq'] < 0


def test_deleting_middle_shot_merges_boundaries_without_confirming(structure_project):
    pid, state = structure_project
    b = state['boundaries']
    confirm(pid, 1, state, 'end-s1')
    state = workflow.get(pid)
    confirm(pid, 2, state, 'start-s3')
    state = workflow.get(pid)
    attach_video(pid, state['shots'][0], b[0]['id'], b[1]['id'])
    attach_video(pid, state['shots'][2], b[2]['id'], b[3]['id'])
    current = workflow.get(pid)
    shots = target(current, [6, 6, 14])
    state = guided_structure.apply(pid, [shots[0], shots[2]], current['revision'])
    middle = state['boundaries'][1]
    assert middle['id'] not in {b[1]['id'], b[2]['id']}
    assert middle['confirmed'] is False
    assert middle['candidate_count'] == 2
    assert state['boundaries'][0]['id'] == b[0]['id']
    assert state['boundaries'][2]['id'] == b[3]['id']
    assert [s['old_video_decision_required'] for s in state['shots']] == [True, True]


def test_inserting_shot_creates_two_unconfirmed_boundaries(structure_project):
    pid, state = structure_project
    confirm(pid, 1, state, 'shared')
    current = workflow.get(pid)
    old = current['shots']
    shots = [
        {'id': old[0]['id'], 'prompt': old[0]['prompt'], 'duration': 6, 'character_ids': []},
        {'id': None, 'prompt': 'An elephant looks up.', 'duration': 4, 'character_ids': []},
        {'id': old[1]['id'], 'prompt': old[1]['prompt'], 'duration': 2, 'character_ids': []},
        {'id': old[2]['id'], 'prompt': old[2]['prompt'], 'duration': 8, 'character_ids': []}]
    state = guided_structure.apply(pid, shots, current['revision'])
    assert [b['confirmed'] for b in state['boundaries']] == [False, False, False, False, False]
    assert state['boundaries'][1]['candidate_count'] == 1
    assert state['boundaries'][2]['candidate_count'] == 1


def test_structure_is_rejected_while_a_paid_request_is_unresolved(structure_project):
    pid, state = structure_project
    with core.connect() as db:
        db.execute("INSERT INTO guided_image_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ('i1', pid, state['boundaries'][0]['id'], 'boundary', 'r1', 'm', 1, 'SUBMITTING', '{}', None, None))
    with pytest.raises(ValueError, match='unresolved'):
        guided_structure.apply(pid, target(state, [6, 6, 8]), state['revision'])
    with core.connect() as db:
        db.execute("UPDATE guided_image_attempts SET status='ACKNOWLEDGED'")
        db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ('v1', pid, state['shots'][0]['id'], 'r2', state['shots'][0]['prompt'], 6, 'minimax', 1, 'RUNNING'))
    with pytest.raises(ValueError, match='unresolved'):
        guided_structure.apply(pid, target(state, [6, 6, 8]), state['revision'])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_structure.py -v`
Expected: FAIL with `ModuleNotFoundError: studio.guided_structure`.

- [ ] **Step 3: Implement**

`apply` starts `BEGIN IMMEDIATE`, calls `timeline.migrate(db)`, checks revision, validates prompts and durations (`provider_capabilities.validate_durations` using the selected i2v model or the project provider), and blocks on unresolved attempts with `ValueError('An unresolved request blocks storyboard changes')`.

For an existing shot whose duration changes: reject if it has `timeline_items`, any `guided_attempts` with `asset_id` or status `COMPLETED`, or `guided_confirmations`.

Archive missing shots: set `archived=1`, `seq = -1 - (count of archived shots in this project)`, delete `timeline_items` and `guided_confirmations` for that shot.

Reindex active shots: add `1_000_000` to all active seqs, then assign `1..N` in request order. Keep original IDs.

Rebuild boundaries: map old active boundaries by `(left_key, right_key)` where left/right are shot IDs or the sentinels `START`/`END`. For each new adjacent pair, reuse the same boundary if the pair is unchanged; otherwise create a boundary and copy candidate versions from the old start boundary of the right shot and the old end boundary of the left shot, deduplicated by `sha256`, all `stale=1`, no confirmation. Call `mark_affected` for every newly created boundary so both neighbors' videos need a decision. Boundaries not reused become `active=0, position=NULL`. Assign positions `0..N`.

Update prompts and durations directly. When `character_ids` differ from the current set, replace `guided_shot_characters` rows, delete that shot's `guided_shot_cast_reviewed` row, and call `boundaries.invalidate_for_shot` so Task 3 cascades run before the rebuild. Bump `guided_projects.revision` once at the end.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_structure.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/guided_structure.py ai-video-studio/studio/timeline.py \
  ai-video-studio/studio/guided_workflow.py ai-video-studio/tests/test_guided_structure.py
git commit -m "feat: apply atomic storyboard structure revisions"
```

---

### Task 7: Legacy guided-project backfill

**Files:**
- Modify: `ai-video-studio/studio/guided_boundaries.py`
- Modify: `ai-video-studio/studio/guided_workflow.py` (`get` triggers backfill once)
- Test: `ai-video-studio/tests/test_guided_backfill.py`

**Interfaces:**
- Produces: `backfill_legacy(db, pid: str) -> bool` (True when it created boundaries)
- `get()` calls it for projects with `guided_character_settings`, a confirmed storyboard, and no active boundaries.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guided_backfill.py
import hashlib

import pytest

from studio import core, guided_boundaries as boundaries, guided_workflow as workflow
from tests.helpers import attach_video, make_png


@pytest.fixture
def legacy_project(boundary_project):
    pid, state = boundary_project
    shots = state['shots']
    with core.connect() as db:
        db.execute('DELETE FROM guided_boundaries WHERE project_id=?', (pid,))
        directory = core.HOME / pid / 'guided-images'
        directory.mkdir(parents=True, exist_ok=True)
        for shot in shots:
            path = directory / f'legacy-{shot["seq"]}.png'
            raw = make_png(path, '1536x1024')
            frame_id = f'legacy-frame-{shot["seq"]}'
            db.execute('INSERT INTO guided_frame_versions VALUES (?,?,?,?,?,0)',
                       (frame_id, pid, shot['id'], str(path), hashlib.sha256(raw).hexdigest()))
            db.execute('INSERT INTO guided_frame_details VALUES (?,?,?,?)',
                       (frame_id, 'old prompt', 'old-model', core.now()))
            db.execute('INSERT INTO guided_frame_selection VALUES (?,?,?)', (pid, shot['id'], frame_id))
    attach_video(pid, shots[0], 'legacy-frame-1', 'legacy-frame-1')
    return pid, state


def test_backfill_is_idempotent_and_lossless(legacy_project):
    pid, state = legacy_project
    first = workflow.get(pid)
    second = workflow.get(pid)
    assert first['boundaries'] == second['boundaries']
    assert len(first['boundaries']) == 3
    assert first['boundaries'][0]['candidate_count'] == 1
    assert first['boundaries'][1]['candidate_count'] == 1
    assert first['boundaries'][2]['candidate_count'] == 0
    assert first['shots'][0]['old_video_decision_required'] is True
    assert (core.HOME / pid / 'guided-images' / 'legacy-1.png').exists()
    assert (core.HOME / 'video-1.mp4').exists()


def test_backfilled_3_2_candidate_cannot_be_confirmed_before_crop(legacy_project):
    pid, state = legacy_project
    state = workflow.get(pid)
    candidate = boundaries.list_frames(pid, state['boundaries'][0]['id'])[0]
    with pytest.raises(ValueError):
        boundaries.confirm(pid, state['boundaries'][0]['id'], candidate['id'], state['revision'])
    cropped = boundaries.crop(pid, state['boundaries'][0]['id'], candidate['id'], state['revision'])
    boundaries.confirm(pid, state['boundaries'][0]['id'], cropped['id'], workflow.get(pid)['revision'],
                       allow_stale=True)
```

Build `legacy_project` by writing the old tables directly (they still exist) after `confirm_storyboard`, then deleting any auto-created boundaries to simulate a pre-upgrade project. `attach_video` also creates a selected asset for shot 1 so the stale decision is observable.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_backfill.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

For each active shot, import its confirmed `guided_frame_selection.confirmed_frame_id` version as a candidate on the shot's **start** boundary: new `guided_boundary_versions` row pointing at the same file, `stale=1`, no confirmation; copy `guided_frame_details` and `guided_frame_characters` rows. Leave end boundaries empty. Mark all shots with a selected asset stale. Guard the whole operation on "no active boundaries exist" so repeated calls are no-ops.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_backfill.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/guided_boundaries.py ai-video-studio/studio/guided_workflow.py \
  ai-video-studio/tests/test_guided_backfill.py
git commit -m "feat: backfill legacy first frames as boundary candidates"
```

---

### Task 8: HTTP routes

**Files:**
- Modify: `ai-video-studio/studio/guided_web.py`
- Test: `ai-video-studio/tests/test_guided_web.py`

**Interfaces:**
- Consumes: all core functions from Tasks 1-7.
- Produces routes:
  - `GET /boundaries`
  - `GET /boundaries/{boundary_id}/frames`
  - `POST /boundaries/{boundary_id}/frames/preview`
  - `POST /boundaries/{boundary_id}/frames`
  - `POST /boundaries/{boundary_id}/frames/{image_id}/confirm`
  - `POST /boundaries/{boundary_id}/frames/{image_id}/crop`
  - `DELETE /boundaries/{boundary_id}/confirm`
  - `PUT /structure`
- Removes `/shots/{seq}/frames*` routes.

- [ ] **Step 1: Write the failing tests**

```python
from studio import guided_boundaries as boundaries, guided_workflow as workflow
from tests.helpers import add_version


def make_boundary_project(api):
    response = api.post('/api/production/projects', json={
        'prompt': 'Elephant in forest', 'duration': 10, 'provider': 'mock', 'budget': 0, 'guided': True})
    pid = response.json()['project_id']
    base = f'/api/projects/{pid}/guided'
    response = api.post(base + '/characters/empty-confirm', json={'revision': 0})
    response = api.put(base + '/draft', json={'revision': response.json()['revision'], 'draft': {
        'character_bible': {}, 'shots': [{'prompt': 'An elephant walks.', 'duration': 10, 'character_ids': []}]}})
    response = api.post(base + '/storyboard/confirm', json={'revision': response.json()['revision']})
    return pid, response.json(), base


def test_boundary_routes(api):
    pid, state, base = make_boundary_project(api)
    list_response = api.get(base + '/boundaries')
    assert list_response.status_code == 200
    assert [b['position'] for b in list_response.json()] == [0, 1]
    for position in (0, 1):
        response = api.post(base + f"/boundaries/{state['boundaries'][position]['id']}/frames/preview", json={})
        assert response.status_code == 200, response.text
    assert api.get(base + '/shots/1/frames').status_code == 404


def test_structure_route_rejects_invalid_total(api):
    pid, state, base = make_boundary_project(api)
    body = {'revision': state['revision'], 'shots': [
        {'id': state['shots'][0]['id'], 'prompt': 'An elephant walks.', 'duration': 9, 'character_ids': []}]}
    assert api.put(base + '/structure', json=body).status_code == 400
    assert workflow.get(pid)['shots'][0]['duration'] == 10


def test_unconfirm_route(api):
    pid, state, base = make_boundary_project(api)
    boundary = state['boundaries'][0]['id']
    version = add_version(pid, boundary, 'b0')
    boundaries.confirm(pid, boundary, version, workflow.get(pid)['revision'])
    response = api.delete(base + f'/boundaries/{boundary}/confirm',
                          json={'revision': workflow.get(pid)['revision']})
    assert response.status_code == 200
    assert workflow.get(pid)['boundaries'][0]['confirmed'] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_web.py -v`
Expected: FAIL with 404 on the new routes.

- [ ] **Step 3: Implement**

Add Pydantic models for `Structure` and `BoundaryConfirm` (optional `allow_stale`). Wrap all core calls in `call(...)` for consistent 400 responses. Keep `GET /` returning the new `boundaries` and per-shot boundary IDs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ai-video-studio && uv run pytest tests/test_guided_web.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/studio/guided_web.py ai-video-studio/tests/test_guided_web.py
git commit -m "feat: expose boundary and structure routes"
```

---

### Task 9: Frontend boundary strip, unlocked shots, structure editor

**Files:**
- Modify: `ai-video-studio/web/src/guidedHelpers.js`
- Modify: `ai-video-studio/web/src/guidedHelpers.test.js`
- Modify: `ai-video-studio/web/src/GuidedProductionWorkspace.jsx`
- Create: `ai-video-studio/web/src/GuidedBoundaryFrames.jsx`
- Delete: `ai-video-studio/web/src/GuidedFirstFrames.jsx`
- Modify: `ai-video-studio/web/src/style.css` (boundary strip and grid classes)

**Interfaces:**
- Consumes: `GET /` state (`boundaries`, per-shot `start_boundary_id` / `end_boundary_id`), boundary routes, `PUT /structure`.
- Produces:
  - `shotReady(shot, boundaries) -> bool`
  - `frameVideoReady(enabled, storyboardConfirmed, shot, videoModel, boundaries) -> bool`
  - `GuidedBoundaryFrames({pid, boundary, state, call, run, refresh})`

- [ ] **Step 1: Write the failing helper test**

```js
test('a shot is ready when both shared boundaries are confirmed', () => {
  const boundaries = [{id:'b0',confirmed:true},{id:'b1',confirmed:false},{id:'b2',confirmed:true}];
  const shot = {start_boundary_id:'b0', end_boundary_id:'b1'};
  assert.equal(shotReady(shot, boundaries), false);
  assert.equal(shotReady({...shot, end_boundary_id:'b2'}, boundaries), true);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ai-video-studio/web && node --test src/guidedHelpers.test.js`
Expected: FAIL, `shotReady is not a function`.

- [ ] **Step 3: Implement helpers and UI**

Replace `accessibleShot` with `shotReady`; update `frameVideoReady` to require `shotReady`. In the workspace: render a boundary strip `B0 镜头1 B1 …`, enable every shot button, show status badges from boundary state, render `GuidedBoundaryFrames` for the selected boundary, and add an "调整分镜结构" panel that submits `PUT /structure` with the full shot list. Disable duration inputs for shots with a selected asset and show "归档并重建". Keep the server error text visible.

Boundary confirm must show a `window.confirm` naming the two affected shots and stating that old videos are preserved but marked stale.

- [ ] **Step 4: Run tests and build**

Run: `cd ai-video-studio/web && node --test src/guidedHelpers.test.js && npm run build`
Expected: tests pass and Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/web/src/guidedHelpers.js ai-video-studio/web/src/guidedHelpers.test.js \
  ai-video-studio/web/src/GuidedProductionWorkspace.jsx ai-video-studio/web/src/GuidedBoundaryFrames.jsx \
  ai-video-studio/web/src/GuidedFirstFrames.jsx ai-video-studio/web/src/style.css
git commit -m "feat: show shared boundaries and unlock shot navigation"
```

---

### Task 10: Full-suite verification and cleanup

**Files:**
- Modify: stale tests that still assume per-shot frames or ordered confirmation (`tests/test_guided_images.py`, `tests/test_guided_workflow.py`, `tests/test_guided_i2v.py`)
- Test: full suite

- [ ] **Step 1: Remove per-shot frame tests and ordered-confirmation assertions**

Delete tests that call `guided_images.preview_frame` / `generate_frame` / `confirm_frame`, and remove those four functions (plus `_frame_snapshot_current`) from `guided_images.py`. Replace `test_confirm_sequence_and_invalidate_edited_shot_only` with a test asserting that confirming shot 1 works while shot 2 is unconfirmed, and that editing shot 1 still invalidates only its own confirmation.

- [ ] **Step 2: Run the full backend suite**

Run: `cd ai-video-studio && uv run pytest -q`
Expected: all tests pass, no network calls.

- [ ] **Step 3: Run the frontend checks**

Run: `cd ai-video-studio/web && node --test src/guidedHelpers.test.js && npm run build`
Expected: pass.

- [ ] **Step 4: Grep for removed names**

Run: `cd ai-video-studio && grep -rn "accessibleShot\|preview_frame\|generate_frame\|confirm_frame\|GuidedFirstFrames" studio web/src tests`
Expected: no matches outside archived docs.

- [ ] **Step 5: Commit**

```bash
git add ai-video-studio/tests ai-video-studio/studio ai-video-studio/web/src
git commit -m "test: align guided suite with shared boundaries"
```

---

## Self-Review

- Spec coverage: persistence (Task 1), generation and 16:9 (Task 2), cascades (Task 3), provider (Task 4), arbitrary order and provenance (Task 5), structure edits (Task 6), legacy backfill (Task 7), routes (Task 8), UI (Task 9), full verification (Task 10).
- Endpoint-boundary sentinel rule is implemented in Task 6's pair map (`START`/`END`).
- Unresolved image and video attempts both block structure edits in Task 6.
- Non-character guided projects keep T2V and get `boundaries: null` in Task 1.
- Upload endpoints are intentionally absent; crop is the only way to normalize a legacy candidate (Task 2).
