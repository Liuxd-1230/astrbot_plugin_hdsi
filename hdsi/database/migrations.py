"""SQLite schema for HDSI (11 domain tables + scheduler bookkeeping).

Table and column names keep parity with the Koishi original so the
migration/export tool can map rows 1:1. Timestamps are stored as ISO-8601
UTC text; JSON columns are TEXT.
"""

SCHEMA_VERSION = 2

TABLES: tuple[str, ...] = (
    "interlude_story",
    "interlude_participant",
    "interlude_script_entry",
    "interlude_memory",
    "interlude_intent",
    "interlude_scene",
    "interlude_arc",
    "interlude_fact",
    "interlude_state_patch",
    "interlude_overlay_snapshot",
    "interlude_web_observation",
)

DDL = """
CREATE TABLE IF NOT EXISTS hdsi_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS interlude_story (
  id TEXT PRIMARY KEY,
  platform_id TEXT NOT NULL DEFAULT '',
  self_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  setting TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}',
  cursor_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_story_status ON interlude_story(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_story_platform ON interlude_story(platform_id, self_id);

CREATE TABLE IF NOT EXISTS interlude_participant (
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL,
  platform_id TEXT NOT NULL DEFAULT '',
  self_id TEXT NOT NULL DEFAULT '',
  session_key TEXT NOT NULL DEFAULT '',
  umo TEXT NOT NULL DEFAULT '',
  message_type TEXT NOT NULL DEFAULT 'FriendMessage',
  person_id TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  relationship TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_participant_story ON interlude_participant(story_id, status);
CREATE INDEX IF NOT EXISTS idx_participant_umo ON interlude_participant(umo);

CREATE TABLE IF NOT EXISTS interlude_script_entry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'life',
  actor TEXT NOT NULL DEFAULT 'character',
  content TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  delivery_intent_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_entry_story_time ON interlude_script_entry(story_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_entry_kind ON interlude_script_entry(story_id, kind);
-- P0-1(v2): idempotent finalize — one spoken fact per staged delivery, ever.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entry_delivery_intent
  ON interlude_script_entry(delivery_intent_id)
  WHERE delivery_intent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS interlude_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'fact',
  content TEXT NOT NULL DEFAULT '',
  importance REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'active',
  source_entry_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_story ON interlude_memory(story_id, status, importance DESC);

CREATE TABLE IF NOT EXISTS interlude_intent (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'follow-up',
  summary TEXT NOT NULL DEFAULT '',
  not_before TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_due ON interlude_intent(story_id, status, not_before);
CREATE INDEX IF NOT EXISTS idx_intent_type ON interlude_intent(story_id, status, type);

CREATE TABLE IF NOT EXISTS interlude_scene (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hook TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  entry_count INTEGER NOT NULL DEFAULT 0,
  last_entry_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scene_story ON interlude_scene(story_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS interlude_arc (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  scene_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arc_story ON interlude_arc(story_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS interlude_fact (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'world',
  content TEXT NOT NULL DEFAULT '',
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.5,
  unresolved INTEGER NOT NULL DEFAULT 0,
  embedding TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  source_entry_ids TEXT NOT NULL DEFAULT '[]',
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_story ON interlude_fact(story_id, status, importance DESC);

CREATE TABLE IF NOT EXISTS interlude_state_patch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT 'character',
  path TEXT NOT NULL DEFAULT '',
  proposed_value TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  impact TEXT NOT NULL DEFAULT 'minor',
  status TEXT NOT NULL DEFAULT 'proposed',
  source_entry_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_patch_story ON interlude_state_patch(story_id, status, confidence DESC);

CREATE TABLE IF NOT EXISTS interlude_overlay_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT 'character',
  tier TEXT NOT NULL DEFAULT 'weekly',
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  major_events TEXT NOT NULL DEFAULT '[]',
  source_patch_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_story ON interlude_overlay_snapshot(story_id, status, target, period_end DESC);

CREATE TABLE IF NOT EXISTS interlude_web_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL,
  participant_id TEXT NOT NULL DEFAULT '',
  intent_id INTEGER,
  mode TEXT NOT NULL DEFAULT 'visit',
  query TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'success',
  accessed_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_story ON interlude_web_observation(story_id, status, accessed_at DESC);
"""
