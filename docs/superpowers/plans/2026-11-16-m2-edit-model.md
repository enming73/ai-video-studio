# M2 Edit Model and Reproducible Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal revisioned storyboard/timeline model and immutable render snapshots, while keeping old projects readable, selectable, and exportable through the existing API/CLI.

**Architecture:** Add additive SQLite tables for storyboard/timeline revisions, shot versions, project state, decisions, and render snapshots. `shots.id` stays stable; content is versioned in `shot_revisions`. Timeline edits copy-on-write into new revisions; `timeline_items` becomes a compatibility projection of the current timeline revision. RenderSnapshots pin both revisions, asset hashes, audio/subtitle hashes, and encoding; the existing `local_render` Job executes them. M2 consumes existing M1-generated assets and does not rewire generation.

**Tech Stack:** Python 3.11+, SQLite/WAL, existing migrations, Job/Worker, FFmpeg/FFprobe, FastAPI, React compatibility endpoints, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-m2-edit-model-design.md`; `docs/superpowers/specs/2026-11-15-studio-product-direction-design.md` §4.3.

## Global Constraints

- Additive schema only. Never rename/drop legacy tables or rewrite existing media files.
- `shots.id` is stable across all revisions.
- Old revisions and RenderSnapshots never read current mutable `shots`/`timeline_items` values.
- Time in persisted revisions is integer microseconds; 24fps is the M2 default.
- `timeline_items` is a compatibility projection, not truth after M2 writes.
- Existing `guided_projects.revision` stays a UI concurrency token; content truth is `storyboard_revisions/timeline_revisions`. Do not replace the integer token in existing React contracts.
- Guided human confirmation is policy-layer; automatic mode must not fabricate `human` decisions.
- M2 does not rewire video generation; it consumes immutable existing assets.
- Tests use local media/fake providers; no real paid API calls.
- Preserve existing URLs and response fields; new fields are additive.

## Review Focus

1. **Old project loses shots/assets during legacy materialization** → Task 1 `test_legacy_materialization_preserves_shot_ids_and_assets`.
2. **Snapshot re-export reads current edits instead of frozen inputs** → Task 5 `test_snapshot_reexport_uses_frozen_inputs_after_edit`.
3. **A subtitle/music edit invalidates or regenerates video** → Task 4 `test_audio_and_subtitle_edits_only_invalidate_mix_and_export`.
4. **Timeline gap/overlap/out-of-bounds is accepted** → Task 3 `test_timeline_validation_rejects_gap_overlap_and_source_overflow`.
5. **Old React selection endpoint bypasses revisions** → Task 3 `test_legacy_selection_writes_revision_and_projection`.
6. **Automatic mode gets guided readiness or fake human decisions** → Task 4 `test_automatic_mode_uses_system_decisions_without_fake_human_confirmation`.
7. **Snapshot output overwrites history** → Task 5 `test_render_snapshot_never_overwrites_previous_output`.

---

### Task 1: Schema, project state, and legacy materialization

**Files:**
- Create: `studio/migrations/005_m2_edit_model.sql`
- Create: `studio/migrations/006_m2_project_state.py`
- Create: `studio/edit_model.py`
- Modify: `studio/timeline.py` (read-compatible helpers only)
- Modify: `tests/test_migrations.py`
- Create: `tests/test_edit_model.py`

**Interfaces:**
- `edit_model.ensure_state(pid, db=None) -> dict` creates `project_state` if absent.
- `edit_model.ensure_legacy_revisions(pid, db=None) -> dict`
  - Idempotently creates one storyboard revision (`revision_no=1`, `source='legacy'`, `actor='legacy'`), one default scene, `shot_revisions` for every existing non-archived shot with existing IDs, one timeline revision (`source='legacy'`), and video/audio/subtitle clips derived from `timeline_items`, `audio-events.json`, and sound config.
  - Returns `{'storyboard_revision_id','timeline_revision_id'}`.
  - Does not alter `shots`, `shot_assets`, media files, or old `render_history`.
- `edit_model.current(pid, db=None) -> dict` returns current pointers and revisions; lazily materializes legacy state.
- Migration `006` only initializes `project_state` rows for existing projects; it does **not** probe media or create revisions. Legacy materialization stays lazy on first current/read/write access, so migration cannot fail on missing FFprobe/media.
- Compatibility projection `edit_model.sync_legacy_projection(pid, db=None)` rewrites `timeline_items` from the current timeline revision; it is the only new writer of `timeline_items`.

- [ ] **Step 1: Write the failing migration/materialization test**

```python
def test_legacy_materialization_preserves_shot_ids_and_assets(legacy_project):
    pid, shot_ids, asset_ids = legacy_project
    revisions = edit_model.ensure_legacy_revisions(pid)
    assert set(shot_ids) <= {r['shot_id'] for r in shot_revisions(pid)}
    assert set(asset_ids) <= {c['asset_id'] for c in current_video_clips(pid)}
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM shots WHERE project_id=?', (pid,)).fetchone()[0] == len(shot_ids)
        assert db.execute('SELECT COUNT(*) FROM render_history WHERE project_id=?', (pid,)).fetchone()[0] >= 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_edit_model.py -k legacy_materialization`
Expected: FAIL with missing `edit_model` / tables.

- [ ] **Step 3: Add additive migration and `edit_model`**

Create all tables from the design: `storyboard_revisions`, `scenes`, `shot_revisions`, `timeline_revisions`, `timeline_video_clips`, `timeline_audio_clips`, `subtitle_revisions`, `decisions`, `render_snapshots`, `project_state`. Add only nullable columns where needed. Do not modify/delete old tables. The Python migration only inserts missing `project_state` rows for existing projects; it must not call FFprobe or create revisions.

- [ ] **Step 4: Add the idempotency test**

```python
def test_legacy_materialization_is_idempotent(legacy_project):
    first = edit_model.ensure_legacy_revisions(pid)
    second = edit_model.ensure_legacy_revisions(pid)
    assert first == second
    with core.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM storyboard_revisions WHERE project_id=?', (pid,)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM timeline_revisions WHERE project_id=?', (pid,)).fetchone()[0] == 1
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra dev --extra web pytest -q tests/test_edit_model.py tests/test_migrations.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/migrations/005_m2_edit_model.sql studio/migrations/006_m2_project_state.py \
  studio/edit_model.py studio/timeline.py tests/test_edit_model.py tests/test_migrations.py
git commit -m "feat: add m2 revision schema and legacy materialization"
```

---

### Task 2: Storyboard revisions, shot versions, and decisions

**Files:**
- Create: `studio/storyboard_revisions.py`
- Modify: `studio/guided_workflow.py`
- Modify: `studio/guided_structure.py`
- Modify: `studio/guided_web.py`
- Modify: `studio/webapp.py`
- Create: `tests/test_storyboard_revisions.py`

**Interfaces:**
- `storyboard_revisions.current(pid) -> dict`
- `storyboard_revisions.create_draft(pid, *, actor='human', source='manual') -> dict`
- `storyboard_revisions.update_shot(pid, revision_id, shot_id, *, prompt=None, duration=None, actor='human') -> dict`
  - Copy-on-write: creates a new `storyboard_revisions` row and clones all `shot_revisions`; only the target shot changes. Old revision rows remain.
- `storyboard_revisions.reorder(pid, revision_id, ordered_shot_ids, *, actor='human') -> dict`
- `storyboard_revisions.record_decision(pid, *, node_type, node_id, action, actor, metadata=None) -> dict`
- Existing Guided `edit_shot`, `structure`, and confirmations write decisions and revision rows; legacy `guided_*` tables remain readable and may be updated as compatibility projections only. `guided_projects.revision` stays the integer UI concurrency token; new revision IDs are content truth.
- New API endpoints from the design; existing Guided shot/structure URLs remain.

- [ ] **Step 1: Write the failing copy-on-write test**

```python
def test_storyboard_edit_creates_new_revision_and_preserves_old(storyboard_revision):
    pid, rev = storyboard_revision
    old = storyboard_revisions.current(pid)
    new = storyboard_revisions.update_shot(pid, old['id'], shot_id, prompt='Changed', actor='human')
    assert new['id'] != old['id']
    assert storyboard_revisions.get(pid, old['id'])['shots'][0]['prompt'] == 'Original'
    assert storyboard_revisions.get(pid, new['id'])['shots'][0]['prompt'] == 'Changed'
    assert shot_id in {s['shot_id'] for s in storyboard_revisions.get(pid, new['id'])['shots']}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_storyboard_revisions.py -k copy_on_write`
Expected: FAIL with missing `storyboard_revisions`.

- [ ] **Step 3: Implement storyboard revisions and decisions**

Add `decisions` records for `system`/`human` accept/reject/keep_old/confirm/select. Backfill existing guided confirmations as human decisions in the legacy materialization path.

- [ ] **Step 4: Add the API/Guided compatibility test**

```python
def test_guided_shot_edit_writes_revision_and_decision(api, guided_storyboard):
    before = storyboard_revisions.current(pid)
    response = api.patch(f'/api/projects/{pid}/guided/shots/1',
                         json={'prompt': 'Changed', 'revision': workflow.get(pid)['revision']})
    assert response.status_code == 200
    after = storyboard_revisions.current(pid)
    assert after['id'] != before['id']
    assert any(d['action'] == 'accept' and d['actor'] == 'human' for d in after['decisions'])
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_storyboard_revisions.py tests/test_guided_workflow.py tests/test_guided_structure.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/storyboard_revisions.py studio/guided_workflow.py studio/guided_structure.py \
  studio/guided_web.py studio/webapp.py tests/test_storyboard_revisions.py
git commit -m "feat: add storyboard revisions and decisions"
```

---

### Task 3: Timeline v1 revisions and legacy projection

**Files:**
- Create: `studio/timeline_revisions.py`
- Modify: `studio/timeline.py`
- Modify: `studio/sound.py`
- Modify: `studio/webapp.py`
- Create: `tests/test_timeline_revisions.py`
- Modify: `tests/test_timeline.py`

**Interfaces:**
- `timeline_revisions.current(pid) -> dict`
- `timeline_revisions.create_draft(pid, *, actor='human') -> dict`
- `timeline_revisions.set_video_clip(pid, revision_id, shot_id, asset_id, *, source_in_us, source_out_us, position, actor='human') -> dict`
- `timeline_revisions.add_audio_clip(...) -> dict`
- `timeline_revisions.set_subtitles(...) -> dict`
- `timeline_revisions.validate(pid, revision_id) -> list[dict]` returns errors for source overflow, gaps, overlaps, invalid duration, missing assets.
- `timeline.timeline(pid)` reads the current revision and returns the existing React shape; `timeline_items` is refreshed via `edit_model.sync_legacy_projection`.
- `timeline.select(pid, seq, asset_id)` creates a new timeline revision and then refreshes the projection; it never writes `timeline_items` as truth.
- Audio/subtitle edits do not touch video clips or assets.

- [ ] **Step 1: Write the failing validation tests**

```python
@pytest.mark.parametrize('source_in,source_out,position,error', [
    (0, 10_000_000, 0, 'source exceeds asset'),
    (0, 2_000_000, 1, 'gap'),
    (0, 2_000_000, 0, 'overlap'),
])
def test_timeline_validation_rejects_gap_overlap_and_source_overflow(
        timeline_revision, source_in, source_out, position, error):
    with pytest.raises(ValueError, match=error):
        timeline_revisions.set_video_clip(pid, rid, shot_id, asset_id,
                                          source_in_us=source_in,
                                          source_out_us=source_out,
                                          position=position)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev --extra web pytest -q tests/test_timeline_revisions.py -k validation`
Expected: FAIL with missing `timeline_revisions`.

- [ ] **Step 3: Implement timeline revisions and validation**

Use microsecond integers internally. Validation is table-driven and runs before writing a new revision. `source_out - source_in` must equal `timeline_out - timeline_in`.

- [ ] **Step 4: Add the legacy selection compatibility test**

```python
def test_legacy_selection_writes_revision_and_projection(timeline_project):
    before = edit_model.current(pid)['timeline_revision_id']
    timeline.select(pid, 1, asset_id)
    after = edit_model.current(pid)['timeline_revision_id']
    assert after != before
    with core.connect() as db:
        assert db.execute('SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?',
                          (pid, shot_id)).fetchone()[0] == asset_id
    assert timeline_revisions.get(pid, after)['video_clips'][0]['asset_id'] == asset_id
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_timeline_revisions.py tests/test_timeline.py tests/test_audio_events.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/timeline_revisions.py studio/timeline.py studio/sound.py studio/webapp.py \
  tests/test_timeline_revisions.py tests/test_timeline.py tests/test_audio_events.py
git commit -m "feat: add timeline revisions and compatibility projection"
```

---

### Task 4: Dependency invalidation and readiness policy

**Files:**
- Create: `studio/edit_readiness.py`
- Modify: `studio/guided_characters.py`
- Modify: `studio/guided_boundaries.py`
- Modify: `studio/guided_generation.py`
- Modify: `studio/storyboard_revisions.py`
- Modify: `studio/timeline_revisions.py`
- Create: `tests/test_edit_readiness.py`

**Interfaces:**
- `edit_readiness.invalidate(pid, *, node_type, node_id, reason, db=None) -> list[str]`
- `edit_readiness.status(pid, *, mode='guided') -> dict`
  - Returns per-shot `valid|stale|blocked`, decision requirements, dependency reasons, and top-level `video_stale`, `mix_stale`, `export_stale` flags used by the tests.
- `edit_readiness.require_ready(pid, *, mode='guided', shot_ids=None)` raises a clear error listing blockers.
- Dependency edges:
  - prompt/duration → shot asset validity and timeline clip timing;
  - reference/character/boundary → affected shot asset validity only;
  - scene/shot order → timeline positions;
  - video clip source range → render only;
  - audio/subtitle → mix/export only;
  - render snapshot → never invalidated by later edits.
- Guided mode requires human confirmation at declared nodes; automatic mode uses system/human decisions but never fabricates human.

- [ ] **Step 1: Write the failing invalidation matrix test**

```python
def test_audio_and_subtitle_edits_only_invalidate_mix_and_export(readiness_project):
    before = edit_readiness.status(pid, mode='guided')
    timeline_revisions.add_audio_clip(...)
    timeline_revisions.set_subtitles(...)
    after = edit_readiness.status(pid, mode='guided')
    assert after['shots'] == before['shots']
    assert after['mix_stale'] and after['export_stale']
    assert not after['video_stale']
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_edit_readiness.py -k audio_and_subtitle`
Expected: FAIL with missing `edit_readiness`.

- [ ] **Step 3: Implement invalidation/readiness and policy split**

Store reasons in decisions/events; do not delete assets or old revisions. Human confirmation is required only under guided policy.

- [ ] **Step 4: Add the automatic-mode test**

```python
def test_automatic_mode_uses_system_decisions_without_fake_human_confirmation(readiness_project):
    decisions = storyboard_revisions.record_decision(pid, node_type='shot', node_id=shot_id,
                                                     action='accept', actor='system')
    status = edit_readiness.status(pid, mode='automatic')
    assert status['shots'][0]['ready'] is True
    assert all(d['actor'] != 'human' for d in status['decisions'])
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_edit_readiness.py tests/test_guided_characters.py tests/test_guided_boundaries.py tests/test_guided_generation.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/edit_readiness.py studio/guided_characters.py studio/guided_boundaries.py \
  studio/guided_generation.py studio/storyboard_revisions.py studio/timeline_revisions.py \
  tests/test_edit_readiness.py
git commit -m "feat: add dependency invalidation and readiness policy"
```

---

### Task 5: RenderSnapshot and local_render Job

**Files:**
- Create: `studio/render_snapshots.py`
- Create: `studio/handlers/local_render.py`
- Modify: `studio/timeline.py`
- Modify: `studio/sound.py`
- Modify: `studio/webapp.py`
- Modify: `studio/cli.py`
- Create: `tests/test_render_snapshots.py`

**Interfaces:**
- `render_snapshots.create(pid, *, timeline_revision_id=None, actor='human') -> dict`
  - Requires a valid timeline revision; writes a `PENDING` snapshot with frozen manifest and enqueues a `local_render` Job.
- `render_snapshots.get(pid, snapshot_id) -> dict`
- `render_snapshots.manifest(pid, snapshot_id) -> dict`
- `render_snapshots.run_and_wait(pid, snapshot_id) -> dict` (CLI/test helper; waits via the Worker and returns the snapshot)
- `render_snapshots.reexport(pid, snapshot_id) -> dict` (renders from the frozen manifest to a new output path; never reads current revisions)
- `handlers/local_render.execute(job, attempts, control)` renders from the frozen manifest, writes a new output, marks snapshot `READY` or `FAILED`.
- `timeline.render(pid, ...)` remains as a compatibility wrapper that creates a snapshot and waits/returns its output; old `/renders` endpoints map to snapshots.
- `sound.export` reads the snapshot output and frozen audio/subtitle inputs, never current revisions.
- Snapshot manifest includes input hashes and can be re-exported without rereading mutable project state.

- [ ] **Step 1: Write the failing snapshot reproducibility test**

```python
def test_snapshot_reexport_uses_frozen_inputs_after_edit(snapshot_project):
    first = render_snapshots.create(pid, actor='human')
    first_output = render_snapshots.run_and_wait(pid, first['id'])['output']
    timeline_revisions.set_subtitles(pid, new_revision, srt_file=other_srt)
    second = render_snapshots.get(pid, first['id'])
    assert second['manifest']['subtitle']['sha256'] == first['manifest']['subtitle']['sha256']
    assert render_snapshots.reexport(pid, first['id'])['output'] != first_output
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev --extra web pytest -q tests/test_render_snapshots.py -k reexport`
Expected: FAIL with missing `render_snapshots`.

- [ ] **Step 3: Implement snapshot creation and the local_render handler**

Snapshot creation validates the current revision and freezes inputs. Rendering reads only the snapshot manifest. Never overwrite an existing output; every export gets a unique path or explicit READY reuse.

- [ ] **Step 4: Add compatibility tests**

```python
def test_legacy_render_endpoint_returns_snapshot_id(api, render_project):
    response = api.post(f'/api/projects/{pid}/renders', json={'source': 'selected'})
    assert response.status_code == 202
    assert response.json()['id']
    assert render_snapshots.get(pid, response.json()['id'])['status'] in ('PENDING', 'READY')
```

- [ ] **Step 5: Run focused tests**

Run: `uv run --extra dev --extra web pytest -q tests/test_render_snapshots.py tests/test_timeline.py tests/test_sound.py tests/test_job_web.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add studio/render_snapshots.py studio/handlers/local_render.py studio/timeline.py \
  studio/sound.py studio/webapp.py studio/cli.py tests/test_render_snapshots.py
git commit -m "feat: add reproducible render snapshots"
```

---

### Task 6: Cross-mode regression, documentation, and acceptance

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`
- Modify: `tests/test_storyboard_revisions.py`
- Modify: `tests/test_timeline_revisions.py`
- Modify: `tests/test_edit_readiness.py`
- Modify: `tests/test_render_snapshots.py`
- Create: `docs/acceptance/m2-edit-model.md`

**Interfaces:**
- No new production interface.
- Existing API/CLI read paths stay compatible; new fields are additive.
- Acceptance record maps every M2 exit condition to a test or manual check.

- [ ] **Step 1: Add cross-mode regression tests**

```python
def test_guided_and_automatic_share_revision_truth(guided_project, automatic_project):
    for pid in (guided_project, automatic_project):
        current = edit_model.current(pid)
        assert current['storyboard_revision_id']
        assert current['timeline_revision_id']
        assert storyboard_revisions.get(pid, current['storyboard_revision_id'])
        assert timeline_revisions.get(pid, current['timeline_revision_id'])
```

- [ ] **Step 2: Run the test to verify it fails or exposes missing compatibility**

Run: `uv run --extra dev --extra web pytest -q tests/test_storyboard_revisions.py -k share_revision_truth`
Expected: FAIL if one mode lacks current revisions.

- [ ] **Step 3: Update docs and acceptance record**

Document:
- revision/snapshot behavior;
- old project read/select/export compatibility;
- no video regeneration on subtitle/music changes;
- snapshot immutability;
- automatic/guided actor rules;
- known M2 limits (no multi-video-track, no effects, no generation rewiring).

- [ ] **Step 4: Run full verification**

Run: `uv run --extra dev --extra web pytest -q`
Expected: all Studio tests pass.

Run: `cd ../vbench && uv run --extra test pytest -q`
Expected: 8 passed.

Run: `cd ../ai-video-studio/web && npm ci && npm run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add README.md ROADMAP.md docs/acceptance/m2-edit-model.md \
  tests/test_storyboard_revisions.py tests/test_timeline_revisions.py \
  tests/test_edit_readiness.py tests/test_render_snapshots.py
git commit -m "docs: record m2 edit model acceptance"
```

---

## Self-Review

- **Spec coverage:** M2.1 storyboard/scene/shot revisions; M2.2 timeline v1; M2.3 render snapshots and local_render; M2.4 invalidation/readiness; mode/actor policy; legacy compatibility. No generation rewiring.
- **Type consistency:** `storyboard_revision_id`/`timeline_revision_id` are the only revision identifiers; `shot_id` remains stable; persisted times use `*_us`.
- **Review Focus:** every line has a named test in its owning task.
- **Proportion:** the plan specifies schema boundaries and compatibility contracts, not implementation bodies; provider/media logic is reused from M1.
