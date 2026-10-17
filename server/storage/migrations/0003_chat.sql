-- Chat: conversations with a standing assignment, and their messages.
CREATE TABLE conversations (
  conversation_id TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(project_id),
  agent_id        TEXT NOT NULL REFERENCES agents(agent_id),
  branch_id       TEXT NOT NULL,
  task_id         TEXT NOT NULL,
  title           TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX idx_conversations_project ON conversations(project_id, updated_at);

CREATE TABLE chat_messages (
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  seq             INTEGER NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','command')),
  text            TEXT NOT NULL,
  run_id          TEXT,
  meta_json       TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  PRIMARY KEY (conversation_id, seq)
);
