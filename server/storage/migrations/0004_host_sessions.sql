-- Sessions of host agents (Claude Code) guarded through hooks: one ANTIBODY run per host session.
CREATE TABLE host_sessions (
  session_id      TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(project_id),
  agent_id        TEXT NOT NULL REFERENCES agents(agent_id),
  run_id          TEXT NOT NULL,
  host            TEXT NOT NULL,
  cwd             TEXT NOT NULL,
  transcript_path TEXT,
  created_at      TEXT NOT NULL,
  last_seen       TEXT NOT NULL
);
CREATE INDEX idx_host_sessions_agent ON host_sessions(agent_id, last_seen);

-- Tool calls decided by ANTIBODY but executed by the host, keyed by the host's tool_use_id.
CREATE TABLE host_tool_calls (
  tool_use_id   TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL,
  decision_id   TEXT NOT NULL,
  operation_id  TEXT NOT NULL,
  outcome       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
