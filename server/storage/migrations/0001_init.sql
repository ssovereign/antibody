-- ANTIBODY schema, migration 1. All timestamps are UTC ISO-8601 strings.
-- Serialized payload columns carry their own schema_version inside the JSON.

CREATE TABLE projects (
  project_id     TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  owner          TEXT NOT NULL,
  policy_head    TEXT,
  registry_head  INTEGER NOT NULL DEFAULT 0,
  settings_json  TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL
);

CREATE TABLE tokens (
  token_hash  TEXT PRIMARY KEY,
  project_id  TEXT REFERENCES projects(project_id),
  role        TEXT NOT NULL CHECK (role IN ('admin','app','worker','operator')),
  actor_id    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE policies (
  project_id  TEXT NOT NULL REFERENCES projects(project_id),
  revision    TEXT NOT NULL,
  body_json   TEXT NOT NULL,
  body_hash   TEXT NOT NULL,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (project_id, revision)
);

-- Operator-controlled tool registry. Effect classification lives here, never in tool annotations.
CREATE TABLE tools (
  project_id            TEXT NOT NULL REFERENCES projects(project_id),
  tool_id               TEXT NOT NULL,
  schema_json           TEXT NOT NULL,
  schema_hash           TEXT NOT NULL,
  effect_class          TEXT NOT NULL CHECK (effect_class IN ('read','read_network','write_local','external_send','payment','delete')),
  executor              TEXT NOT NULL,
  server_id             TEXT,
  learnable_params_json TEXT NOT NULL DEFAULT '{}',
  reconcile             INTEGER NOT NULL DEFAULT 0,
  enabled               INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (project_id, tool_id)
);

CREATE TABLE mcp_servers (
  project_id      TEXT NOT NULL REFERENCES projects(project_id),
  server_id       TEXT NOT NULL,
  command_json    TEXT NOT NULL,
  identity_hash   TEXT NOT NULL,
  pinned_tools_json TEXT NOT NULL,
  trusted         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, server_id)
);

CREATE TABLE recipients (
  project_id    TEXT NOT NULL REFERENCES projects(project_id),
  recipient_id  TEXT NOT NULL,
  address       TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'local_outbox',
  PRIMARY KEY (project_id, recipient_id)
);

CREATE TABLE agents (
  agent_id            TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(project_id),
  name                TEXT NOT NULL,
  adapter             TEXT NOT NULL,
  accepted_branch_id  TEXT,
  config_json         TEXT NOT NULL,
  created_at          TEXT NOT NULL
);

CREATE TABLE branches (
  branch_id         TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(project_id),
  agent_id          TEXT NOT NULL REFERENCES agents(agent_id),
  name              TEXT NOT NULL,
  head_version      TEXT,
  parent_branch_id  TEXT,
  base_version      TEXT,
  workspace_dir     TEXT NOT NULL UNIQUE,
  thread_ns         TEXT NOT NULL UNIQUE,
  state_json        TEXT NOT NULL,
  barrier           INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'active',
  purpose           TEXT NOT NULL DEFAULT 'work',
  created_at        TEXT NOT NULL,
  UNIQUE (agent_id, name)
);

CREATE TABLE tasks (
  project_id  TEXT NOT NULL REFERENCES projects(project_id),
  task_id     TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  goal        TEXT NOT NULL,
  body_json   TEXT NOT NULL,
  body_hash   TEXT NOT NULL,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (project_id, task_id, revision)
);

CREATE TABLE task_heads (
  project_id        TEXT NOT NULL,
  task_id           TEXT NOT NULL,
  current_revision  INTEGER NOT NULL,
  PRIMARY KEY (project_id, task_id)
);

CREATE TABLE grants (
  grant_id          TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(project_id),
  tool_id           TEXT NOT NULL,
  args_hash         TEXT,
  operation_class   TEXT,
  resource_scope_json TEXT NOT NULL DEFAULT '{}',
  expires_at        TEXT NOT NULL,
  remaining_uses    INTEGER NOT NULL,
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL
);

CREATE TABLE runs (
  run_id                TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(project_id),
  agent_id              TEXT NOT NULL REFERENCES agents(agent_id),
  branch_id             TEXT NOT NULL REFERENCES branches(branch_id),
  parent_run_id         TEXT REFERENCES runs(run_id),
  restored_from_version TEXT,
  task_id               TEXT NOT NULL,
  task_revision         INTEGER NOT NULL,
  adapter               TEXT NOT NULL,
  mode                  TEXT NOT NULL CHECK (mode IN ('live','recorded','simulated')),
  status                TEXT NOT NULL,
  status_reason         TEXT,
  health                TEXT NOT NULL DEFAULT 'Healthy',
  policy_revision       TEXT NOT NULL,
  registry_revision     INTEGER NOT NULL,
  base_version          TEXT,
  context_budget_tokens INTEGER NOT NULL,
  start_config_json     TEXT NOT NULL,
  start_config_hash     TEXT NOT NULL,
  thread_id             TEXT NOT NULL UNIQUE,
  steps                 INTEGER NOT NULL DEFAULT 0,
  cost_json             TEXT NOT NULL DEFAULT '{}',
  purpose               TEXT NOT NULL DEFAULT 'task',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX idx_runs_project_status ON runs(project_id, status);

CREATE TABLE events (
  event_id           TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES projects(project_id),
  run_id             TEXT NOT NULL,
  sequence           INTEGER NOT NULL,
  timestamp_utc      TEXT NOT NULL,
  kind               TEXT NOT NULL,
  actor_type         TEXT NOT NULL,
  payload_ref        TEXT,
  payload_json       TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  policy_revision    TEXT,
  registry_revision  INTEGER,
  schema_version     INTEGER NOT NULL DEFAULT 1,
  UNIQUE (run_id, sequence)
);
CREATE INDEX idx_events_run_seq ON events(run_id, sequence);
CREATE INDEX idx_events_project_kind ON events(project_id, kind);

CREATE TABLE sources (
  source_id        TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES projects(project_id),
  channel          TEXT NOT NULL,
  origin           TEXT NOT NULL CHECK (origin IN ('USER_ASSIGNMENT','OPERATOR_POLICY','EXTERNAL_DATA','AGENT_DERIVED','VERIFIED_TOOL_METADATA')),
  content_hash     TEXT NOT NULL,
  raw_ref          TEXT,
  view_ref         TEXT NOT NULL,
  parser           TEXT NOT NULL,
  parser_revision  TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  retrieved_at     TEXT NOT NULL,
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  deps_json        TEXT NOT NULL DEFAULT '[]',
  status           TEXT NOT NULL DEFAULT 'active',
  run_id           TEXT,
  label            TEXT
);
CREATE INDEX idx_sources_hash ON sources(project_id, content_hash);
CREATE INDEX idx_sources_project_status ON sources(project_id, status);

CREATE TABLE chunks (
  source_id     TEXT NOT NULL REFERENCES sources(source_id),
  chunk_index   INTEGER NOT NULL,
  start_offset  INTEGER NOT NULL,
  end_offset    INTEGER NOT NULL,
  text          TEXT NOT NULL,
  text_hash     TEXT NOT NULL,
  norm_hash     TEXT NOT NULL,
  flags_json    TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (source_id, chunk_index)
);
CREATE INDEX idx_chunks_text_hash ON chunks(text_hash);
CREATE INDEX idx_chunks_norm_hash ON chunks(norm_hash);

CREATE TABLE source_exclusions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL REFERENCES sources(source_id),
  kind          TEXT NOT NULL,
  start_offset  INTEGER,
  end_offset    INTEGER,
  detail_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);

CREATE TABLE dispositions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   TEXT NOT NULL,
  run_id       TEXT,
  source_id    TEXT NOT NULL,
  chunk_index  INTEGER,
  disposition  TEXT NOT NULL CHECK (disposition IN ('quarantined','flagged','clean','released')),
  pattern_ref  TEXT,
  reason       TEXT NOT NULL,
  detail_json  TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_dispositions_run ON dispositions(run_id, source_id);

CREATE TABLE log_cursors (
  project_id     TEXT NOT NULL,
  path           TEXT NOT NULL,
  file_identity  TEXT NOT NULL,
  byte_offset    INTEGER NOT NULL,
  line_no        INTEGER NOT NULL,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (project_id, path)
);

CREATE TABLE calendar_events (
  project_id    TEXT NOT NULL,
  provider      TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  calendar_id   TEXT NOT NULL,
  event_uid     TEXT NOT NULL,
  revision      TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  status        TEXT NOT NULL,
  PRIMARY KEY (project_id, provider, calendar_id, event_uid, revision)
);

CREATE TABLE integrations (
  project_id   TEXT NOT NULL,
  integration  TEXT NOT NULL,
  state        TEXT NOT NULL,
  detail_json  TEXT NOT NULL DEFAULT '{}',
  cursor       TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (project_id, integration)
);

CREATE TABLE operations (
  operation_id  TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  action_id     TEXT NOT NULL UNIQUE,
  purpose       TEXT NOT NULL,
  issued_at     TEXT NOT NULL
);

CREATE TABLE operation_bindings (
  run_id              TEXT NOT NULL,
  operation_id        TEXT NOT NULL REFERENCES operations(operation_id),
  inherited_from_run  TEXT NOT NULL,
  PRIMARY KEY (run_id, operation_id)
);

CREATE TABLE actions (
  action_id            TEXT PRIMARY KEY,
  project_id           TEXT NOT NULL,
  run_id               TEXT NOT NULL,
  tool_id              TEXT NOT NULL,
  tool_schema_hash     TEXT NOT NULL,
  args_ref             TEXT NOT NULL,
  args_redacted_json   TEXT NOT NULL,
  args_hash            TEXT NOT NULL,
  operation_id         TEXT NOT NULL,
  observation_refs_json TEXT NOT NULL DEFAULT '[]',
  created_at           TEXT NOT NULL
);
CREATE INDEX idx_actions_run ON actions(run_id);

CREATE TABLE decisions (
  decision_id         TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL,
  action_id           TEXT NOT NULL,
  run_id              TEXT NOT NULL,
  operation_id        TEXT NOT NULL,
  tool_id             TEXT NOT NULL,
  args_hash           TEXT NOT NULL,
  canonical_args_ref  TEXT NOT NULL,
  outcome             TEXT NOT NULL CHECK (outcome IN ('ALLOW','DENY','PAUSE')),
  reason_code         TEXT NOT NULL,
  message             TEXT NOT NULL,
  policy_revision     TEXT NOT NULL,
  task_revision       INTEGER NOT NULL,
  registry_revision   INTEGER NOT NULL,
  expires_at          TEXT NOT NULL,
  evidence_refs_json  TEXT NOT NULL DEFAULT '[]',
  checks_json         TEXT NOT NULL DEFAULT '[]',
  audit_json          TEXT,
  attribution_status  TEXT NOT NULL DEFAULT 'observed',
  grant_id            TEXT,
  created_at          TEXT NOT NULL
);
CREATE INDEX idx_decisions_run ON decisions(run_id);

CREATE TABLE effects (
  project_id    TEXT NOT NULL,
  operation_id  TEXT NOT NULL,
  decision_id   TEXT NOT NULL REFERENCES decisions(decision_id),
  run_id        TEXT NOT NULL,
  branch_id     TEXT NOT NULL,
  tool_id       TEXT NOT NULL,
  effect_class  TEXT NOT NULL,
  args_hash     TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('RESERVED','DISPATCHING','COMPLETED','FAILED','UNKNOWN','DENIED')),
  lease_owner   TEXT,
  lease_expires TEXT,
  claim_revision INTEGER NOT NULL DEFAULT 0,
  external_ref  TEXT,
  result_ref    TEXT,
  error_json    TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  journal_seq   INTEGER NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, operation_id)
);
CREATE INDEX idx_effects_state ON effects(project_id, state);
CREATE INDEX idx_effects_run ON effects(run_id);

CREATE TABLE effect_journal (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    TEXT NOT NULL,
  operation_id  TEXT NOT NULL,
  state         TEXT NOT NULL,
  detail_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);

CREATE TABLE executor_invocations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    TEXT NOT NULL,
  decision_id   TEXT NOT NULL,
  operation_id  TEXT NOT NULL,
  tool_id       TEXT NOT NULL,
  args_hash     TEXT NOT NULL,
  worker_id     TEXT NOT NULL,
  invoked_at    TEXT NOT NULL
);

CREATE TABLE artifacts (
  project_id    TEXT NOT NULL,
  branch_id     TEXT NOT NULL,
  path          TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  run_id        TEXT,
  operation_id  TEXT,
  data_class    TEXT NOT NULL DEFAULT 'unapproved',
  check_status  TEXT NOT NULL DEFAULT 'unchecked',
  check_detail_json TEXT NOT NULL DEFAULT '{}',
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, branch_id, path)
);

CREATE TABLE health_transitions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,
  from_state  TEXT NOT NULL,
  to_state    TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE TABLE failures (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  detail_json  TEXT NOT NULL,
  event_ref    TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_failures_run_fp ON failures(run_id, fingerprint);

CREATE TABLE edges (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  src_ref     TEXT NOT NULL,
  dst_ref     TEXT NOT NULL,
  kind        TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_edges_run ON edges(run_id);
CREATE INDEX idx_edges_dst ON edges(dst_ref);

CREATE TABLE model_calls (
  call_id        TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  packet_id      TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model_id       TEXT NOT NULL,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  count_kind     TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  latency_ms     INTEGER,
  created_at     TEXT NOT NULL
);

CREATE TABLE incidents (
  incident_id        TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL,
  run_id             TEXT NOT NULL,
  category           TEXT NOT NULL,
  severity           TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'open',
  fingerprint        TEXT,
  observed_json      TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  hypotheses_json    TEXT NOT NULL DEFAULT '[]',
  attribution_level  TEXT NOT NULL DEFAULT 'observed',
  candidate_id       TEXT,
  recovery_run_id    TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX idx_incidents_project_status ON incidents(project_id, status);

CREATE TABLE candidates (
  candidate_id     TEXT NOT NULL,
  revision         INTEGER NOT NULL,
  project_id       TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('procedure','threat_pattern','memory_fact','context_repair','harness_patch')),
  status           TEXT NOT NULL CHECK (status IN ('Draft','Testing','Accepted','Rejected','Superseded','Revoked')),
  incident_id      TEXT,
  lesson_id        TEXT NOT NULL,
  parent_revision  INTEGER,
  body_json        TEXT NOT NULL,
  body_hash        TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (candidate_id, revision)
);
CREATE INDEX idx_candidates_project_status ON candidates(project_id, status);

CREATE TABLE lessons (
  project_id         TEXT NOT NULL,
  lesson_id          TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  kind               TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('accepted','superseded','revoked')),
  scope_tool         TEXT,
  body_json          TEXT NOT NULL,
  body_hash          TEXT NOT NULL,
  candidate_id       TEXT NOT NULL,
  candidate_revision INTEGER NOT NULL,
  evaluation_id      TEXT NOT NULL,
  acceptance_mode    TEXT NOT NULL CHECK (acceptance_mode IN ('assisted','automatic')),
  registry_revision  INTEGER NOT NULL,
  accepted_by        TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  PRIMARY KEY (project_id, lesson_id, revision)
);
CREATE INDEX idx_lessons_applicability ON lessons(project_id, status, kind, scope_tool);

-- Accepted bodies are immutable; enforce in the database too.
CREATE TRIGGER lessons_body_immutable BEFORE UPDATE OF body_json, body_hash, kind, scope_tool ON lessons
BEGIN
  SELECT RAISE(ABORT, 'accepted lesson revisions are immutable');
END;

CREATE TABLE revocations (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id         TEXT NOT NULL,
  lesson_id          TEXT NOT NULL,
  revision           INTEGER,
  reason             TEXT NOT NULL,
  actor              TEXT NOT NULL,
  registry_revision  INTEGER NOT NULL,
  created_at         TEXT NOT NULL
);
CREATE INDEX idx_revocations ON revocations(project_id, lesson_id);

CREATE TABLE registry_revisions (
  project_id   TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  changes_json TEXT NOT NULL,
  publisher    TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (project_id, revision)
);

CREATE TABLE registry_acks (
  run_id      TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  boundary    TEXT NOT NULL,
  acked_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, revision)
);

CREATE TABLE lesson_uses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  lesson_ref  TEXT NOT NULL,
  use_kind    TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE TABLE suites (
  suite_id      TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  checker_hash  TEXT NOT NULL,
  cases_hash    TEXT NOT NULL,
  cases_json    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (suite_id, revision)
);

CREATE TABLE evaluations (
  evaluation_id      TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL,
  candidate_id       TEXT,
  candidate_revision INTEGER,
  subject_json       TEXT NOT NULL DEFAULT '{}',
  suite_id           TEXT NOT NULL,
  suite_revision     INTEGER NOT NULL,
  checker_hash       TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('running','passed','failed','incomplete')),
  results_json       TEXT NOT NULL DEFAULT '[]',
  summary_json       TEXT NOT NULL DEFAULT '{}',
  cost_json          TEXT NOT NULL DEFAULT '{}',
  mode               TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  finished_at        TEXT
);

CREATE TABLE versions (
  version_id               TEXT PRIMARY KEY,
  project_id               TEXT NOT NULL,
  branch_id                TEXT NOT NULL,
  manifest_ref             TEXT NOT NULL,
  parents_json             TEXT NOT NULL,
  label                    TEXT,
  requires_reconciliation  INTEGER NOT NULL DEFAULT 0,
  created_by               TEXT NOT NULL,
  created_at               TEXT NOT NULL
);
CREATE INDEX idx_versions_branch ON versions(branch_id);

CREATE TABLE context_packets (
  packet_id         TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  compiler_revision TEXT NOT NULL,
  budget_tokens     INTEGER NOT NULL,
  token_count       INTEGER NOT NULL,
  count_kind        TEXT NOT NULL,
  packet_ref        TEXT NOT NULL,
  included_json     TEXT NOT NULL,
  omitted_json      TEXT NOT NULL,
  valid             INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL
);
CREATE INDEX idx_packets_run ON context_packets(run_id);

CREATE TABLE summaries (
  summary_id  TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  deps_json   TEXT NOT NULL,
  origin      TEXT NOT NULL,
  text        TEXT NOT NULL,
  valid       INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL
);

CREATE TABLE preferences (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL,
  scope       TEXT NOT NULL CHECK (scope IN ('user','agent','project')),
  scope_id    TEXT NOT NULL,
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (project_id, scope, scope_id, key)
);

CREATE TABLE jobs (
  job_id          TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  kind            TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  payload_json    TEXT NOT NULL,
  lease_owner     TEXT,
  lease_expires   TEXT,
  claim_revision  INTEGER NOT NULL DEFAULT 0,
  result_json     TEXT,
  error_json      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status, kind);

CREATE TABLE idempotency (
  project_id    TEXT NOT NULL,
  idem_key      TEXT NOT NULL,
  endpoint      TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  status_code   INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, idem_key, endpoint)
);
