from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

CHANNEL_ID_BOUNDARY = -1_000_000_000_000

SCHEMA = """
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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_channel_id(chat_id: int) -> bool:
    return chat_id < CHANNEL_ID_BOUNDARY


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) else None


@dataclass
class MessageRecord:
    chat_id: int
    message_id: int
    date: str
    sender_id: int | None = None
    sender_name: str | None = None
    sender_username: str | None = None
    outgoing: bool = False
    text: str | None = None
    reply_to: int | None = None
    grouped_id: int | None = None
    media_type: str | None = None
    media_uid: str | None = None
    media_path: str | None = None
    media_size: int | None = None
    media_note: str | None = None
    edited: bool = False
    edit_date: str | None = None


@dataclass(frozen=True)
class Stats:
    messages: int
    chats: int
    users: int
    media_files: int
    deleted: int
    edits: int
    db_bytes: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.create_function("pylower", 1, _lower, deterministic=True)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def upsert_chat(self, chat_id: int, title: str | None, chat_type: str, username: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO chats (id, title, type, username, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = COALESCE(excluded.title, chats.title),
                type = excluded.type,
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, username, utcnow()),
        )
        await self.conn.commit()

    async def upsert_user(
        self,
        user_id: int,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
        phone: str | None,
        is_bot: bool,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (id, first_name, last_name, username, phone, is_bot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                username = excluded.username,
                phone = COALESCE(excluded.phone, users.phone),
                is_bot = excluded.is_bot,
                updated_at = excluded.updated_at
            """,
            (user_id, first_name, last_name, username, phone, int(is_bot), utcnow()),
        )
        await self.conn.commit()

    async def save_message(self, record: MessageRecord) -> None:
        data = asdict(record)
        data["outgoing"] = int(record.outgoing)
        data["edited"] = int(record.edited)
        data["saved_at"] = utcnow()
        columns = ", ".join(data)
        placeholders = ", ".join(f":{key}" for key in data)
        await self.conn.execute(
            f"""
            INSERT INTO messages ({columns}) VALUES ({placeholders})
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                sender_id = excluded.sender_id,
                sender_name = COALESCE(excluded.sender_name, messages.sender_name),
                sender_username = COALESCE(excluded.sender_username, messages.sender_username),
                text = excluded.text,
                reply_to = excluded.reply_to,
                grouped_id = excluded.grouped_id,
                media_type = excluded.media_type,
                media_uid = excluded.media_uid,
                media_path = COALESCE(excluded.media_path, messages.media_path),
                media_size = COALESCE(excluded.media_size, messages.media_size),
                media_note = excluded.media_note,
                edited = MAX(messages.edited, excluded.edited),
                edit_date = COALESCE(excluded.edit_date, messages.edit_date)
            """,
            data,
        )
        await self.conn.commit()

    async def set_media(
        self, chat_id: int, message_id: int, path: str | None, size: int | None, note: str | None
    ) -> None:
        await self.conn.execute(
            "UPDATE messages SET media_path = ?, media_size = COALESCE(?, media_size), media_note = ? "
            "WHERE chat_id = ? AND message_id = ?",
            (path, size, note, chat_id, message_id),
        )
        await self.conn.commit()

    async def get_message(self, chat_id: int, message_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ) as cur:
            return await cur.fetchone()

    async def record_edit(self, old: aiosqlite.Row, new: MessageRecord) -> None:
        await self.conn.execute(
            """
            INSERT INTO edits (chat_id, message_id, old_text, new_text, old_media_type, old_media_path, edited_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old["chat_id"],
                old["message_id"],
                old["text"],
                new.text,
                old["media_type"],
                old["media_path"],
                new.edit_date or utcnow(),
            ),
        )
        await self.conn.commit()

    async def find_for_deletion(self, message_ids: Sequence[int], chat_id: int | None) -> list[aiosqlite.Row]:
        if not message_ids:
            return []
        rows: list[aiosqlite.Row] = []
        for start in range(0, len(message_ids), 500):
            chunk = list(message_ids[start : start + 500])
            marks = ",".join("?" * len(chunk))
            if chat_id is None:
                sql = (
                    f"SELECT * FROM messages WHERE message_id IN ({marks}) "
                    f"AND chat_id > ? AND deleted = 0"
                )
                params = [*chunk, CHANNEL_ID_BOUNDARY]
            else:
                sql = f"SELECT * FROM messages WHERE chat_id = ? AND message_id IN ({marks}) AND deleted = 0"
                params = [chat_id, *chunk]
            async with self.conn.execute(sql, params) as cur:
                rows.extend(await cur.fetchall())
        return rows

    async def mark_deleted(self, row_ids: Iterable[int], when: str | None = None) -> None:
        ids = list(row_ids)
        if not ids:
            return
        when = when or utcnow()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            await self.conn.execute(
                f"UPDATE messages SET deleted = 1, deleted_at = ? WHERE id IN ({marks})", [when, *chunk]
            )
        await self.conn.commit()

    async def get_chat(self, chat_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)) as cur:
            return await cur.fetchone()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            return await cur.fetchone()

    async def find_chat(self, query: str) -> aiosqlite.Row | None:
        query = query.strip()
        if not query:
            return None
        try:
            return await self.get_chat(int(query))
        except ValueError:
            pass
        name = query.lstrip("@").lower()
        async with self.conn.execute(
            """
            SELECT c.* FROM chats c
            LEFT JOIN users u ON u.id = c.id
            WHERE pylower(c.username) = ? OR pylower(u.username) = ?
            ORDER BY c.updated_at DESC LIMIT 1
            """,
            (name, name),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return row
        async with self.conn.execute(
            "SELECT * FROM chats WHERE pylower(title) LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"%{query.lower()}%",),
        ) as cur:
            return await cur.fetchone()

    async def chat_messages(self, chat_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY date, message_id", (chat_id,)
        ) as cur:
            return list(await cur.fetchall())

    async def chat_edits(self, chat_id: int) -> dict[int, list[aiosqlite.Row]]:
        result: dict[int, list[aiosqlite.Row]] = {}
        async with self.conn.execute(
            "SELECT * FROM edits WHERE chat_id = ? ORDER BY edited_at, id", (chat_id,)
        ) as cur:
            async for row in cur:
                result.setdefault(row["message_id"], []).append(row)
        return result

    async def deleted_messages(self, chat_id: int, limit: int = 50) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND deleted = 1 ORDER BY deleted_at DESC, date DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def search(self, text: str, limit: int = 20) -> list[aiosqlite.Row]:
        pattern = "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.conn.execute(
            """
            SELECT m.*, c.title AS chat_title FROM messages m
            LEFT JOIN chats c ON c.id = m.chat_id
            WHERE pylower(m.text) LIKE ? ESCAPE '\\'
            ORDER BY m.date DESC LIMIT ?
            """,
            (pattern, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def stats(self) -> Stats:
        async def scalar(sql: str) -> int:
            async with self.conn.execute(sql) as cur:
                row = await cur.fetchone()
                return int(row[0] or 0)

        db_bytes = sum(
            p.stat().st_size for p in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")) if p.exists()
        )
        return Stats(
            messages=await scalar("SELECT COUNT(*) FROM messages"),
            chats=await scalar("SELECT COUNT(*) FROM chats"),
            users=await scalar("SELECT COUNT(*) FROM users"),
            media_files=await scalar("SELECT COUNT(*) FROM messages WHERE media_path IS NOT NULL"),
            deleted=await scalar("SELECT COUNT(*) FROM messages WHERE deleted = 1"),
            edits=await scalar("SELECT COUNT(*) FROM edits"),
            db_bytes=db_bytes,
        )
