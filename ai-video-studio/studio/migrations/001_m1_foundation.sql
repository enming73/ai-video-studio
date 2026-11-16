CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  budget_limit_microusd INTEGER,
  label TEXT,
  created TEXT NOT NULL,
  closed TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  request_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  budget_limit_microusd INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  block_reason TEXT,
  error_class TEXT,
  error_detail TEXT,
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  UNIQUE(project_id, request_key),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, created);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created);

CREATE TABLE IF NOT EXISTS job_attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  remote_id TEXT,
  input_json TEXT NOT NULL,
  output_json TEXT,
  error_class TEXT,
  error_detail TEXT,
  retry_after_sec INTEGER,
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  UNIQUE(job_id, attempt_no),
  UNIQUE(idempotency_key),
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_job ON job_attempts(job_id, attempt_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_active_fingerprint
  ON job_attempts(job_id, request_fingerprint)
  WHERE status IN ('PLANNED','RESERVED','SUBMITTING','SUBMITTED',
                   'POLLING','DOWNLOADING','VALIDATING');

CREATE TABLE IF NOT EXISTS job_events (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  attempt_id TEXT,
  type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  created TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, created);

CREATE TABLE IF NOT EXISTS cost_entries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT,
  job_id TEXT,
  attempt_id TEXT,
  stage TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  pricing_version TEXT,
  reserved_microusd INTEGER NOT NULL DEFAULT 0,
  estimated_microusd INTEGER,
  actual_microusd INTEGER,
  billing_status TEXT NOT NULL,
  evidence TEXT,
  legacy_key TEXT UNIQUE,
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  UNIQUE(attempt_id, stage),
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(run_id) REFERENCES runs(id),
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_costs_project ON cost_entries(project_id, run_id);
CREATE INDEX IF NOT EXISTS idx_costs_attempt ON cost_entries(attempt_id);
