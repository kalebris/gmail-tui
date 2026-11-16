CREATE TABLE sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  email TEXT,
  history_id TEXT,
  open_label TEXT NOT NULL DEFAULT 'INBOX',
  bootstrap_phase TEXT NOT NULL DEFAULT 'need_profile',
  bootstrap_page_token TEXT,
  bootstrap_listed INTEGER NOT NULL DEFAULT 0,
  messages_total INTEGER NOT NULL DEFAULT 0,
  bootstrap_history_id TEXT,
  history_page_token TEXT,
  history_page_start TEXT,
  resync_gen INTEGER NOT NULL DEFAULT 0,
  resyncing INTEGER NOT NULL DEFAULT 0,
  queue_seq INTEGER NOT NULL DEFAULT 0
);

INSERT INTO sync_state (id) VALUES (1);

CREATE TABLE labels (
  id INTEGER PRIMARY KEY,
  gmail_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT ''
);

CREATE TABLE threads (
  id INTEGER PRIMARY KEY,
  gmail_id TEXT NOT NULL UNIQUE,
  snippet TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  from_addr TEXT NOT NULL DEFAULT '',
  latest_internal_date INTEGER NOT NULL DEFAULT 0,
  latest_message_gmail_id TEXT NOT NULL DEFAULT '',
  unread_count INTEGER NOT NULL DEFAULT 0,
  starred INTEGER NOT NULL DEFAULT 0,
  message_count INTEGER NOT NULL DEFAULT 0,
  label_ids_csv TEXT NOT NULL DEFAULT ''
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  gmail_id TEXT NOT NULL UNIQUE,
  history_id TEXT,
  internal_date INTEGER NOT NULL DEFAULT 0,
  snippet TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  from_addr TEXT NOT NULL DEFAULT '',
  to_addr TEXT NOT NULL DEFAULT '',
  cc_addr TEXT NOT NULL DEFAULT '',
  message_id_header TEXT NOT NULL DEFAULT '',
  in_reply_to TEXT NOT NULL DEFAULT '',
  references_header TEXT NOT NULL DEFAULT '',
  size_estimate INTEGER,
  body_state TEXT NOT NULL DEFAULT 'missing'
    CHECK (body_state IN ('missing', 'pending_now', 'present')),
  body_path TEXT,
  has_metadata INTEGER NOT NULL DEFAULT 0,
  has_attachment INTEGER NOT NULL DEFAULT 0,
  resync_gen INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX messages_thread ON messages(thread_id, internal_date DESC, id DESC);
CREATE INDEX messages_backfill ON messages(body_state, internal_date DESC);

CREATE TABLE message_labels (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  internal_date INTEGER NOT NULL,
  PRIMARY KEY (message_id, label_id)
);

CREATE INDEX message_labels_by_label
  ON message_labels(label_id, internal_date DESC, message_id DESC);

CREATE TABLE thread_labels (
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  latest_internal_date INTEGER NOT NULL,
  PRIMARY KEY (thread_id, label_id)
);

CREATE INDEX thread_labels_list
  ON thread_labels(label_id, latest_internal_date DESC, thread_id DESC);

CREATE TABLE mutation_queue (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  label_gmail_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('add', 'remove')),
  seq INTEGER NOT NULL,
  confirmed_history_id TEXT,
  UNIQUE (message_id, label_gmail_id)
);

CREATE TABLE label_creates (
  local_id TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE snoozed (
  gmail_id TEXT PRIMARY KEY,
  until_ms INTEGER NOT NULL
);

CREATE INDEX snoozed_until ON snoozed(until_ms);

CREATE TABLE message_text (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  subject TEXT NOT NULL DEFAULT '',
  from_addr TEXT NOT NULL DEFAULT '',
  to_addr TEXT NOT NULL DEFAULT '',
  cc_addr TEXT NOT NULL DEFAULT '',
  snippet TEXT NOT NULL DEFAULT '',
  body_text TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE message_fts USING fts5(
  subject,
  from_addr,
  to_addr,
  cc_addr,
  snippet,
  body_text,
  content='message_text',
  content_rowid='message_id',
  tokenize='unicode61'
);

CREATE TRIGGER message_text_ai AFTER INSERT ON message_text BEGIN
  INSERT INTO message_fts(
    rowid, subject, from_addr, to_addr, cc_addr, snippet, body_text
  ) VALUES (
    new.message_id, new.subject, new.from_addr, new.to_addr, new.cc_addr,
    new.snippet, new.body_text
  );
END;

CREATE TRIGGER message_text_ad AFTER DELETE ON message_text BEGIN
  INSERT INTO message_fts(
    message_fts, rowid, subject, from_addr, to_addr, cc_addr, snippet, body_text
  ) VALUES (
    'delete', old.message_id, old.subject, old.from_addr, old.to_addr,
    old.cc_addr, old.snippet, old.body_text
  );
END;

CREATE TRIGGER message_text_au AFTER UPDATE ON message_text BEGIN
  INSERT INTO message_fts(
    message_fts, rowid, subject, from_addr, to_addr, cc_addr, snippet, body_text
  ) VALUES (
    'delete', old.message_id, old.subject, old.from_addr, old.to_addr,
    old.cc_addr, old.snippet, old.body_text
  );
  INSERT INTO message_fts(
    rowid, subject, from_addr, to_addr, cc_addr, snippet, body_text
  ) VALUES (
    new.message_id, new.subject, new.from_addr, new.to_addr, new.cc_addr,
    new.snippet, new.body_text
  );
END;
