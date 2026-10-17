-- Operator-declared tool metadata (e.g. which argument names a destination for proxied tools).
ALTER TABLE tools ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}';
