-- Hosted mode: a user's computer runs a small connector. It uploads snapshots of guarded Claude Code
-- folders (files, conversation parts, Claude's memory) and applies changes the server queues for it.
CREATE TABLE host_snapshots (
  agent_id     TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL,
  files_json   TEXT NOT NULL,          -- {rel path: "sha256:..."}
  memory_json  TEXT NOT NULL,          -- {rel path: "sha256:..."}
  client_json  TEXT NOT NULL,          -- {"os", "claude_projects_dir", "memory_dir"}
  updated_at   TEXT NOT NULL
);
CREATE TABLE host_transcripts (
  session_id       TEXT PRIMARY KEY,
  agent_id         TEXT NOT NULL,
  project_id       TEXT NOT NULL,
  parts_json       TEXT NOT NULL,      -- ["sha256:...", ...] fixed line-count parts, in order
  has_conversation INTEGER NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE connector_ops (
  op_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  op_json      TEXT NOT NULL,          -- {"op": "write"|"delete", "path": client path, "blob": ref}
  status       TEXT NOT NULL,          -- pending | delivered
  created_at   TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX idx_connector_ops ON connector_ops(agent_id, status);
CREATE TABLE connector_commands (
  command_id  TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  line        TEXT NOT NULL,
  actor       TEXT NOT NULL,
  status      TEXT NOT NULL,           -- queued | running | done
  result_json TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE connectors (
  project_id  TEXT NOT NULL,
  name        TEXT NOT NULL,
  os          TEXT,
  last_seen   TEXT NOT NULL,
  PRIMARY KEY (project_id, name)
);
CREATE TABLE users (
  user_id      TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  provider_uid TEXT NOT NULL,
  login        TEXT NOT NULL,
  name         TEXT,
  avatar_url   TEXT,
  project_id   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_login   TEXT NOT NULL,
  UNIQUE (provider, provider_uid)
);
CREATE TABLE claude_usage (
  project_id TEXT NOT NULL,
  day        TEXT NOT NULL,
  calls      INTEGER NOT NULL,
  PRIMARY KEY (project_id, day)
);
