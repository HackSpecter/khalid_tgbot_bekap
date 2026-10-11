from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from .migrations import migrate

log = logging.getLogger(__name__)

CHANNEL_ID_BOUNDARY = -1_000_000_000_000
PLAINTEXT_HEADER = b"SQLite format 3\x00"

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
    view_once: bool = False
    edited: bool = False
    edit_date: str | None = None
    fwd_from_id: int | None = None
    fwd_from_name: str | None = None
    fwd_date: str | None = None
    reactions: str | None = None
    service_action: str | None = None


@dataclass(frozen=True)
class Stats:
    messages: int
    chats: int
    users: int
    media_files: int
    deleted: int
    edits: int
    db_bytes: int
    evidence: int = 0
    profiles: int = 0
    anchors: int = 0
    encrypted: bool = False


def _cipher_driver():
    try:
        import sqlcipher3
    except ImportError as exc:
        raise RuntimeError("ENCRYPTION_KEY is set but sqlcipher3 is not installed") from exc
    return sqlcipher3


def is_plaintext_db(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as fh:
        return fh.read(16) == PLAINTEXT_HEADER


def encrypt_plaintext_db(path: Path, key_hex: str) -> None:
    driver = _cipher_driver()
    tmp = path.with_name(path.name + ".encrypting")
    if tmp.exists():
        tmp.unlink()
    plain = driver.connect(str(path))
    try:
        plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        plain.execute(f"ATTACH DATABASE '{tmp}' AS encrypted KEY \"x'{key_hex}'\"")
        plain.execute("SELECT sqlcipher_export('encrypted')")
        version = plain.execute("PRAGMA user_version").fetchone()[0]
        plain.execute(f"PRAGMA encrypted.user_version = {int(version)}")
        plain.execute("DETACH DATABASE encrypted")
    finally:
        plain.close()
    backup = path.with_name(path.name + ".plaintext-backup")
    os.replace(path, backup)
    for suffix in ("-wal", "-shm"):
        leftover = Path(f"{path}{suffix}")
        if leftover.exists():
            leftover.unlink()
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    backup.unlink()
    log.warning("Database %s converted to SQLCipher", path)


class Database:
    def __init__(self, path: Path, key_hex: str | None = None) -> None:
        self.path = Path(path)
        self.key_hex = key_hex
        self._conn: aiosqlite.Connection | None = None

    @property
    def encrypted(self) -> bool:
        return self.key_hex is not None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    def _connector(self):
        if self.key_hex is None:
            return sqlite3, lambda: sqlite3.connect(self.path)
        driver = _cipher_driver()
        key_hex = self.key_hex

        def connect():
            conn = driver.connect(str(self.path))
            conn.execute(f"PRAGMA key = \"x'{key_hex}'\"")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn

        return driver, connect

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_hex is not None and is_plaintext_db(self.path):
            encrypt_plaintext_db(self.path, self.key_hex)
        driver, connector = self._connector()
        self._conn = aiosqlite.Connection(connector, 64)
        await self._conn
        self._conn.row_factory = driver.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.create_function("pylower", 1, _lower, deterministic=True)
        await migrate(self._conn)
        if self.path.exists():
            os.chmod(self.path, 0o600)

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
        data["view_once"] = int(record.view_once)
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
                edit_date = COALESCE(excluded.edit_date, messages.edit_date),
                fwd_from_id = excluded.fwd_from_id,
                fwd_from_name = COALESCE(excluded.fwd_from_name, messages.fwd_from_name),
                fwd_date = excluded.fwd_date,
                reactions = COALESCE(excluded.reactions, messages.reactions),
                service_action = excluded.service_action
            """,
            data,
        )
        await self.conn.commit()

    async def set_media(
        self,
        chat_id: int,
        message_id: int,
        path: str | None,
        size: int | None,
        note: str | None,
        sha256: str | None = None,
        thumb_path: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE messages SET media_path = ?, media_size = COALESCE(?, media_size), media_note = ?, "
            "media_sha256 = ?, thumb_path = ? WHERE chat_id = ? AND message_id = ?",
            (path, size, note, sha256, thumb_path, chat_id, message_id),
        )
        await self.conn.commit()

    async def link_evidence(self, chat_id: int, message_id: int, seq: int, record_hash: str) -> None:
        await self.conn.execute(
            "UPDATE messages SET first_seq = ?, record_hash = ? "
            "WHERE chat_id = ? AND message_id = ? AND first_seq IS NULL",
            (seq, record_hash, chat_id, message_id),
        )
        await self.conn.commit()

    async def set_reactions(self, chat_id: int, message_id: int, reactions: str | None) -> None:
        await self.conn.execute(
            "UPDATE messages SET reactions = ? WHERE chat_id = ? AND message_id = ?",
            (reactions, chat_id, message_id),
        )
        await self.conn.commit()

    async def unread_outgoing(self, chat_id: int, max_id: int) -> list[int]:
        async with self.conn.execute(
            "SELECT message_id FROM messages WHERE chat_id = ? AND outgoing = 1 AND read_at IS NULL "
            "AND message_id <= ? AND (media_type IS NULL OR media_type != 'service') ORDER BY message_id",
            (chat_id, max_id),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def mark_read(self, chat_id: int, message_ids: Sequence[int], read_at: str, seq: int) -> None:
        for start in range(0, len(message_ids), 500):
            chunk = list(message_ids[start : start + 500])
            marks = ",".join("?" * len(chunk))
            await self.conn.execute(
                f"UPDATE messages SET read_at = ?, read_seq = ? WHERE chat_id = ? AND message_id IN ({marks}) "
                "AND read_at IS NULL",
                [read_at, seq, chat_id, *chunk],
            )
        await self.conn.commit()

    async def get_message(self, chat_id: int, message_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ) as cur:
            return await cur.fetchone()

    async def record_edit(self, old: aiosqlite.Row, new: MessageRecord, evidence_seq: int | None = None) -> None:
        await self.conn.execute(
            """
            INSERT INTO edits (chat_id, message_id, old_text, new_text, old_media_type, old_media_path,
                               old_media_sha256, evidence_seq, edited_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old["chat_id"],
                old["message_id"],
                old["text"],
                new.text,
                old["media_type"],
                old["media_path"],
                old["media_sha256"],
                evidence_seq,
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

    async def scalar(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0) if row else 0

    async def stats(self) -> Stats:
        db_bytes = sum(
            p.stat().st_size for p in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")) if p.exists()
        )
        return Stats(
            messages=await self.scalar("SELECT COUNT(*) FROM messages"),
            chats=await self.scalar("SELECT COUNT(*) FROM chats"),
            users=await self.scalar("SELECT COUNT(*) FROM users"),
            media_files=await self.scalar("SELECT COUNT(*) FROM messages WHERE media_path IS NOT NULL"),
            deleted=await self.scalar("SELECT COUNT(*) FROM messages WHERE deleted = 1"),
            edits=await self.scalar("SELECT COUNT(*) FROM edits"),
            db_bytes=db_bytes,
            evidence=await self.scalar("SELECT COUNT(*) FROM evidence_log"),
            profiles=await self.scalar("SELECT COUNT(*) FROM profile_history"),
            anchors=await self.scalar("SELECT COUNT(*) FROM anchors"),
            encrypted=self.encrypted,
        )

    async def kv_get(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )
        await self.conn.commit()

    async def last_profile(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM profile_history WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def profile_history(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM profile_history WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cur:
            return list(await cur.fetchall())

    async def add_profile_snapshot(self, snapshot: dict) -> None:
        columns = ", ".join(snapshot)
        placeholders = ", ".join(f":{key}" for key in snapshot)
        await self.conn.execute(f"INSERT INTO profile_history ({columns}) VALUES ({placeholders})", snapshot)
        await self.conn.execute(
            "UPDATE users SET bio = COALESCE(?, bio), photo_id = ? WHERE id = ?",
            (snapshot.get("bio"), snapshot.get("photo_id"), snapshot["user_id"]),
        )
        await self.conn.commit()

    async def active_private_users(self, since_iso: str) -> list[int]:
        async with self.conn.execute(
            """
            SELECT DISTINCT m.chat_id FROM messages m
            JOIN chats c ON c.id = m.chat_id AND c.type = 'private'
            WHERE m.chat_id > 0 AND m.date >= ?
            """,
            (since_iso,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def add_anchor(self, anchor: dict) -> int:
        columns = ", ".join(anchor)
        placeholders = ", ".join(f":{key}" for key in anchor)
        cur = await self.conn.execute(f"INSERT INTO anchors ({columns}) VALUES ({placeholders})", anchor)
        await self.conn.commit()
        return cur.lastrowid

    async def update_anchor(self, anchor_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        await self.conn.execute(f"UPDATE anchors SET {assignments} WHERE id = :id", {**fields, "id": anchor_id})
        await self.conn.commit()

    async def anchors(self) -> list[aiosqlite.Row]:
        async with self.conn.execute("SELECT * FROM anchors ORDER BY id") as cur:
            return list(await cur.fetchall())

    async def last_anchor(self) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM anchors ORDER BY id DESC LIMIT 1") as cur:
            return await cur.fetchone()

    async def messages_without_evidence(self, limit: int = 1000) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE first_seq IS NULL ORDER BY id LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())
