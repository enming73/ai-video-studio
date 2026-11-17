-- M2 additive schema: revisioned storyboard/timeline, project state, decisions,
-- and immutable render snapshots. This migration only adds tables and nullable
-- columns; legacy tables (projects, shots, shot_assets, timeline_items,
-- render_history) are never renamed, dropped, or rewritten.
--
-- Persisted revision times use integer microseconds and carry the `_us` suffix.
-- `shot_revisions.duration` mirrors `shots.duration` (integer seconds) and has
-- no `_us` suffix by design.

CREATE TABLE IF NOT EXISTS storyboard_revisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL,
  source TEXT NOT NULL,
  actor TEXT NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(project_id, revision_no),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_storyboard_revisions_project
  ON storyboard_revisions(project_id, revision_no);

CREATE TABLE IF NOT EXISTS scenes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  storyboard_revision_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  title TEXT,
  description TEXT,
  created TEXT NOT NULL,
  UNIQUE(storyboard_revision_id, seq),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(storyboard_revision_id) REFERENCES storyboard_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_scenes_storyboard ON scenes(storyboard_revision_id, seq);

CREATE TABLE IF NOT EXISTS shot_revisions (
  id TEXT PRIMARY KEY,
  storyboard_revision_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  scene_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  duration INTEGER NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(storyboard_revision_id, shot_id),
  UNIQUE(storyboard_revision_id, scene_id, seq),
  FOREIGN KEY(storyboard_revision_id) REFERENCES storyboard_revisions(id),
  FOREIGN KEY(shot_id) REFERENCES shots(id),
  FOREIGN KEY(scene_id) REFERENCES scenes(id)
);
CREATE INDEX IF NOT EXISTS idx_shot_revisions_storyboard
  ON shot_revisions(storyboard_revision_id, seq);

CREATE TABLE IF NOT EXISTS timeline_revisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  storyboard_revision_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL,
  fps INTEGER NOT NULL DEFAULT 24,
  duration_us INTEGER NOT NULL,
  source TEXT NOT NULL,
  actor TEXT NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(project_id, revision_no),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(storyboard_revision_id) REFERENCES storyboard_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_timeline_revisions_project
  ON timeline_revisions(project_id, revision_no);

CREATE TABLE IF NOT EXISTS project_state (
  project_id TEXT PRIMARY KEY,
  current_storyboard_revision_id TEXT,
  current_timeline_revision_id TEXT,
  updated TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS timeline_video_clips (
  id TEXT PRIMARY KEY,
  timeline_revision_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  source_in_us INTEGER NOT NULL,
  source_out_us INTEGER NOT NULL,
  timeline_in_us INTEGER NOT NULL,
  timeline_out_us INTEGER NOT NULL,
  created TEXT NOT NULL,
  UNIQUE(timeline_revision_id, position),
  FOREIGN KEY(timeline_revision_id) REFERENCES timeline_revisions(id),
  FOREIGN KEY(shot_id) REFERENCES shots(id),
  FOREIGN KEY(asset_id) REFERENCES shot_assets(id)
);
CREATE INDEX IF NOT EXISTS idx_video_clips_timeline
  ON timeline_video_clips(timeline_revision_id, position);

CREATE TABLE IF NOT EXISTS timeline_audio_clips (
  id TEXT PRIMARY KEY,
  timeline_revision_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  file TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  source_in_us INTEGER NOT NULL,
  source_out_us INTEGER NOT NULL,
  timeline_in_us INTEGER NOT NULL,
  timeline_out_us INTEGER NOT NULL,
  gain REAL NOT NULL,
  fade_in_us INTEGER NOT NULL DEFAULT 0,
  fade_out_us INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  FOREIGN KEY(timeline_revision_id) REFERENCES timeline_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_audio_clips_timeline
  ON timeline_audio_clips(timeline_revision_id);

CREATE TABLE IF NOT EXISTS subtitle_revisions (
  id TEXT PRIMARY KEY,
  timeline_revision_id TEXT NOT NULL,
  srt_file TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  language TEXT,
  created TEXT NOT NULL,
  FOREIGN KEY(timeline_revision_id) REFERENCES timeline_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_subtitle_revisions_timeline
  ON subtitle_revisions(timeline_revision_id);

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  node_type TEXT NOT NULL,
  node_id TEXT NOT NULL,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id, created);

CREATE TABLE IF NOT EXISTS render_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  timeline_revision_id TEXT NOT NULL,
  storyboard_revision_id TEXT NOT NULL,
  subtitle_revision_id TEXT,
  status TEXT NOT NULL,
  output TEXT,
  manifest_json TEXT NOT NULL,
  fps INTEGER NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  video_codec TEXT NOT NULL,
  audio_codec TEXT NOT NULL,
  created TEXT NOT NULL,
  ready TEXT,
  error TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(timeline_revision_id) REFERENCES timeline_revisions(id),
  FOREIGN KEY(storyboard_revision_id) REFERENCES storyboard_revisions(id),
  FOREIGN KEY(subtitle_revision_id) REFERENCES subtitle_revisions(id)
);
CREATE INDEX IF NOT EXISTS idx_render_snapshots_project
  ON render_snapshots(project_id, created);
