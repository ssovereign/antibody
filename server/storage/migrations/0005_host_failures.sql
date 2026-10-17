-- Failed commands seen in host agents (Claude Code), grouped by what kind of mistake they are,
-- so repeated mistakes become lessons.
CREATE TABLE host_failures (
  failure_id   TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  session_id   TEXT,
  tool         TEXT NOT NULL,
  command      TEXT NOT NULL,
  error        TEXT NOT NULL,
  family       TEXT NOT NULL,
  key          TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  incident_id  TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_host_failures_fp ON host_failures(agent_id, fingerprint);
