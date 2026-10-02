from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator

GENESIS_HASH = "0" * 64

EVENT_NEW = "new"
EVENT_EDIT = "edit"
EVENT_DELETE = "delete"
EVENT_REACTION = "reaction"
EVENT_MEDIA = "media"
EVENT_PROFILE = "profile_change"
EVENT_ANCHOR = "anchor"
EVENT_READ = "read"


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return repr(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_default)


def raw_dict(obj: Any) -> dict | None:
    if obj is None:
        return None
    try:
        return json.loads(canonical_json(obj.to_dict()))
    except Exception:
        return {"_repr": repr(obj)}


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_entry_hash(seq: int, prev_hash: str, record_hash: str) -> str:
    return hashlib.sha256(f"{seq}:{prev_hash}:{record_hash}".encode()).hexdigest()


def observed_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Entry:
    seq: int
    event: str
    chat_id: int | None
    message_id: int | None
    observed_at: str
    payload: str
    record_hash: str
    prev_hash: str
    entry_hash: str
    media_sha256: str | None = None
    media_path: str | None = None

    @classmethod
    def from_row(cls, row) -> "Entry":
        return cls(
            seq=row["seq"],
            event=row["event"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            observed_at=row["observed_at"],
            payload=row["payload"],
            record_hash=row["record_hash"],
            prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
            media_sha256=row["media_sha256"],
            media_path=row["media_path"],
        )

    def header(self) -> dict:
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
            "entry_hash": self.entry_hash,
        }

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "event": self.event,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "observed_at": self.observed_at,
            "payload": self.payload,
            "record_hash": self.record_hash,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


@dataclass
class ChainIssue:
    seq: int
    problem: str


class ChainVerifier:
    def __init__(self, start_prev: str | None = GENESIS_HASH) -> None:
        self.expected_prev = start_prev
        self.expected_seq: int | None = None
        self.count = 0
        self.issues: list[ChainIssue] = []
        self.last: Entry | None = None

    def feed(self, entry: Entry) -> None:
        self.count += 1
        if self.expected_seq is not None and entry.seq != self.expected_seq:
            self.issues.append(ChainIssue(entry.seq, f"разрыв нумерации: ожидался seq {self.expected_seq}"))
        if self.expected_prev is not None and entry.prev_hash != self.expected_prev:
            self.issues.append(ChainIssue(entry.seq, "prev_hash не совпадает с хешем предыдущей записи"))
        if hash_text(entry.payload) != entry.record_hash:
            self.issues.append(ChainIssue(entry.seq, "record_hash не совпадает с содержимым записи"))
        elif json.loads(entry.payload).get("media_sha256") != entry.media_sha256:
            self.issues.append(ChainIssue(entry.seq, "media_sha256 в таблице не совпадает с записью"))
        if compute_entry_hash(entry.seq, entry.prev_hash, entry.record_hash) != entry.entry_hash:
            self.issues.append(ChainIssue(entry.seq, "entry_hash вычислен неверно"))
        self.expected_prev = entry.entry_hash
        self.expected_seq = entry.seq + 1
        self.last = entry


def verify_entries(entries: Iterable[Entry], *, start_prev: str | None = GENESIS_HASH) -> ChainVerifier:
    verifier = ChainVerifier(start_prev)
    for entry in entries:
        verifier.feed(entry)
    return verifier


class EvidenceLog:
    MAX_RETRIES = 8

    def __init__(self, db) -> None:
        self.db = db
        self._lock = asyncio.Lock()

    async def head(self) -> tuple[int, str]:
        async with self.db.conn.execute(
            "SELECT seq, entry_hash FROM evidence_log ORDER BY seq DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return (row["seq"], row["entry_hash"]) if row else (0, GENESIS_HASH)

    async def append(
        self,
        event: str,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        raw: Any = None,
        data: dict | None = None,
        media_sha256: str | None = None,
        media_path: str | None = None,
        observed_at: str | None = None,
    ) -> Entry:
        observed_at = observed_at or observed_now()
        payload = canonical_json(
            {
                "event": event,
                "chat_id": chat_id,
                "message_id": message_id,
                "observed_at": observed_at,
                "raw": raw,
                "media_sha256": media_sha256,
                "data": data or {},
            }
        )
        record_hash = hash_text(payload)
        async with self._lock:
            for attempt in range(self.MAX_RETRIES):
                last_seq, prev_hash = await self.head()
                seq = last_seq + 1
                entry = Entry(
                    seq=seq,
                    event=event,
                    chat_id=chat_id,
                    message_id=message_id,
                    observed_at=observed_at,
                    payload=payload,
                    record_hash=record_hash,
                    prev_hash=prev_hash,
                    entry_hash=compute_entry_hash(seq, prev_hash, record_hash),
                    media_sha256=media_sha256,
                    media_path=media_path,
                )
                try:
                    await self.db.conn.execute(
                        """
                        INSERT INTO evidence_log
                            (seq, event, chat_id, message_id, observed_at, payload, record_hash, prev_hash,
                             entry_hash, media_sha256, media_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry.seq,
                            entry.event,
                            entry.chat_id,
                            entry.message_id,
                            entry.observed_at,
                            entry.payload,
                            entry.record_hash,
                            entry.prev_hash,
                            entry.entry_hash,
                            entry.media_sha256,
                            entry.media_path,
                        ),
                    )
                    await self.db.conn.commit()
                    return entry
                except Exception as exc:
                    if not _is_conflict(exc) or attempt == self.MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("unreachable")

    def iter_all(self, start: int = 1, end: int | None = None, batch: int = 2000) -> "AsyncEntryIterator":
        return AsyncEntryIterator(self.db, start, end, batch)

    async def for_chat(self, chat_id: int) -> list[Entry]:
        async with self.db.conn.execute(
            "SELECT * FROM evidence_log WHERE chat_id = ? ORDER BY seq", (chat_id,)
        ) as cur:
            return [Entry.from_row(row) for row in await cur.fetchall()]

    async def entry_at(self, seq: int) -> Entry | None:
        async with self.db.conn.execute("SELECT * FROM evidence_log WHERE seq = ?", (seq,)) as cur:
            row = await cur.fetchone()
        return Entry.from_row(row) if row else None


def _is_conflict(exc: Exception) -> bool:
    text = str(exc).lower()
    return "unique" in text or "evidence chain" in text or "locked" in text or "busy" in text


class AsyncEntryIterator:
    def __init__(self, db, start: int, end: int | None, batch: int) -> None:
        self.db = db
        self.next_seq = start
        self.end = end
        self.batch = batch

    def __aiter__(self) -> "AsyncEntryIterator":
        self._buffer: Iterator[Entry] = iter(())
        return self

    async def __anext__(self) -> Entry:
        try:
            return next(self._buffer)
        except StopIteration:
            pass
        if self.end is not None and self.next_seq > self.end:
            raise StopAsyncIteration
        async with self.db.conn.execute(
            "SELECT * FROM evidence_log WHERE seq >= ? AND seq <= ? ORDER BY seq LIMIT ?",
            (self.next_seq, self.end if self.end is not None else 2**62, self.batch),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            raise StopAsyncIteration
        self.next_seq = rows[-1]["seq"] + 1
        self._buffer = iter([Entry.from_row(row) for row in rows])
        return next(self._buffer)
