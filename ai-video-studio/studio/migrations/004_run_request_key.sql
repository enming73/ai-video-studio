ALTER TABLE runs ADD COLUMN request_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_request_key
  ON runs(project_id, request_key) WHERE request_key IS NOT NULL;
ALTER TABLE jobs ADD COLUMN target_key TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(target_key, status);
