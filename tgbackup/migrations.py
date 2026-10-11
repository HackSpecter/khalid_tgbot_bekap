from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

V1_BASE = """
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY,
    title       TEXT,
    type        TEXT NOT NULL,
    username    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT,
    last_name   TEXT,
    username    TEXT,
    phone       TEXT,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    sender_id       INTEGER,
    sender_name     TEXT,
    sender_username TEXT,
    outgoing        INTEGER NOT NULL DEFAULT 0,
    text            TEXT,
    date            TEXT NOT NULL,
    reply_to        INTEGER,
    grouped_id      INTEGER,
    media_type      TEXT,
    media_uid       TEXT,
    media_path      TEXT,
    media_size      INTEGER,
    media_note      TEXT,
    edited          INTEGER NOT NULL DEFAULT 0,
    edit_date       TEXT,
    deleted         INTEGER NOT NULL DEFAULT 0,
    deleted_at      TEXT,
    saved_at        TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_messages_message_id ON messages (message_id);
CREATE INDEX IF NOT EXISTS ix_messages_chat_date ON messages (chat_id, date);
CREATE INDEX IF NOT EXISTS ix_messages_deleted ON messages (chat_id, deleted);

CREATE TABLE IF NOT EXISTS edits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    old_text        TEXT,
    new_text        TEXT,
    old_media_type  TEXT,
    old_media_path  TEXT,
    edited_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_edits_message ON edits (chat_id, message_id);
"""

V2_EVIDENCE = """
ALTER TABLE messages ADD COLUMN media_sha256 TEXT;
ALTER TABLE messages ADD COLUMN fwd_from_id INTEGER;
ALTER TABLE messages ADD COLUMN fwd_from_name TEXT;
ALTER TABLE messages ADD COLUMN fwd_date TEXT;
ALTER TABLE messages ADD COLUMN reactions TEXT;
ALTER TABLE messages ADD COLUMN service_action TEXT;
ALTER TABLE messages ADD COLUMN first_seq INTEGER;
ALTER TABLE messages ADD COLUMN record_hash TEXT;

ALTER TABLE edits ADD COLUMN old_media_sha256 TEXT;
ALTER TABLE edits ADD COLUMN evidence_seq INTEGER;

ALTER TABLE users ADD COLUMN bio TEXT;
ALTER TABLE users ADD COLUMN photo_id INTEGER;

CREATE TABLE evidence_log (
    seq             INTEGER PRIMARY KEY,
    event           TEXT NOT NULL,
    chat_id         INTEGER,
    message_id      INTEGER,
    observed_at     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    record_hash     TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL UNIQUE,
    media_sha256    TEXT,
    media_path      TEXT
);

CREATE INDEX ix_evidence_chat ON evidence_log (chat_id, message_id);
CREATE INDEX ix_evidence_event ON evidence_log (event);

CREATE TABLE profile_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    observed_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    first_name      TEXT,
    last_name       TEXT,
    username        TEXT,
    phone           TEXT,
    bio             TEXT,
    photo_id        INTEGER,
    photo_path      TEXT,
    photo_sha256    TEXT,
    changes         TEXT NOT NULL,
    evidence_seq    INTEGER
);

CREATE INDEX ix_profile_user ON profile_history (user_id, id);

CREATE TABLE anchors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    entry_hash      TEXT NOT NULL,
    anchor_file     TEXT NOT NULL,
    anchor_sha256   TEXT NOT NULL,
    tsr_file        TEXT,
    tsa_time        TEXT,
    tsa_error       TEXT,
    ots_file        TEXT,
    ots_status      TEXT,
    ots_block       INTEGER,
    ots_block_time  TEXT,
    ots_checked_at  TEXT,
    ots_error       TEXT
);

CREATE TABLE kv (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER evidence_log_no_update BEFORE UPDATE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;
CREATE TRIGGER evidence_log_no_delete BEFORE DELETE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;

CREATE TRIGGER edits_no_update BEFORE UPDATE ON edits
BEGIN SELECT RAISE(ABORT, 'edits is append-only'); END;
CREATE TRIGGER edits_no_delete BEFORE DELETE ON edits
BEGIN SELECT RAISE(ABORT, 'edits is append-only'); END;

CREATE TRIGGER profile_history_no_update BEFORE UPDATE ON profile_history
BEGIN SELECT RAISE(ABORT, 'profile_history is append-only'); END;
CREATE TRIGGER profile_history_no_delete BEFORE DELETE ON profile_history
BEGIN SELECT RAISE(ABORT, 'profile_history is append-only'); END;

CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are never deleted'); END;

CREATE TRIGGER messages_immutable_original BEFORE UPDATE OF chat_id, message_id, date, outgoing, first_seq, record_hash ON messages
WHEN OLD.first_seq IS NOT NULL AND (
    NEW.chat_id IS NOT OLD.chat_id OR NEW.message_id IS NOT OLD.message_id OR NEW.date IS NOT OLD.date
    OR NEW.outgoing IS NOT OLD.outgoing OR NEW.first_seq IS NOT OLD.first_seq OR NEW.record_hash IS NOT OLD.record_hash
)
BEGIN SELECT RAISE(ABORT, 'original message identity is immutable'); END;
"""


V3_GUARDS_READS = """
CREATE TRIGGER evidence_log_chain_guard BEFORE INSERT ON evidence_log
WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM evidence_log), 0) + 1
    OR NEW.prev_hash != COALESCE(
        (SELECT entry_hash FROM evidence_log WHERE seq = NEW.seq - 1),
        '0000000000000000000000000000000000000000000000000000000000000000'
    )
BEGIN SELECT RAISE(ABORT, 'evidence chain broken: stale head'); END;

ALTER TABLE messages ADD COLUMN read_at TEXT;
ALTER TABLE messages ADD COLUMN read_seq INTEGER;
CREATE INDEX ix_messages_unread_out ON messages (chat_id, outgoing, read_at);
"""


V4_THUMBS = """
ALTER TABLE messages ADD COLUMN thumb_path TEXT;
"""


V5_VIEW_ONCE = """
ALTER TABLE messages ADD COLUMN view_once INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_messages_view_once ON messages (view_once);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = [
    Migration(1, "base schema", V1_BASE),
    Migration(2, "evidence chain, profiles, anchors", V2_EVIDENCE),
    Migration(3, "chain guard trigger, read receipts", V3_GUARDS_READS),
    Migration(4, "media thumbnails", V4_THUMBS),
    Migration(5, "view-once capture flag", V5_VIEW_ONCE),
]

LATEST = MIGRATIONS[-1].version


async def current_version(conn) -> int:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT NOT NULL)"
    )
    async with conn.execute("SELECT MAX(version) FROM schema_migrations") as cur:
        row = await cur.fetchone()
    if row[0] is not None:
        return int(row[0])
    async with conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'") as cur:
        legacy = await cur.fetchone()
    if legacy:
        await conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'base schema (pre-existing)', datetime('now'))"
        )
        await conn.commit()
        return 1
    return 0


async def migrate(conn) -> list[int]:
    version = await current_version(conn)
    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        log.info("Applying migration %s: %s", migration.version, migration.name)
        await conn.execute("BEGIN")
        try:
            for statement in split_sql(migration.sql):
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, datetime('now'))",
                (migration.version, migration.name),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        applied.append(migration.version)
    return applied


def split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.endswith("END;"):
                statements.append("\n".join(buffer))
                buffer = []
                in_trigger = False
        elif stripped.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements
