from __future__ import annotations

import asyncio
import logging

from .crypto import ENC_SUFFIX, Vault, sha256_hex
from .db import Database
from .evidence import EVENT_NEW, EvidenceLog

log = logging.getLogger(__name__)

BACKFILL_FIELDS = (
    "chat_id", "message_id", "sender_id", "sender_name", "sender_username", "outgoing", "text", "date",
    "reply_to", "grouped_id", "media_type", "media_uid", "media_path", "media_size", "media_note",
    "edited", "edit_date", "deleted", "deleted_at", "saved_at",
)


async def encrypt_legacy_media(db: Database, vault: Vault) -> int:
    if vault.key is None:
        return 0
    async with db.conn.execute(
        "SELECT id, media_path FROM messages WHERE media_path IS NOT NULL AND media_path NOT LIKE ?",
        (f"%{ENC_SUFFIX}",),
    ) as cur:
        rows = await cur.fetchall()
    converted = 0
    for row in rows:
        if not vault.exists(row["media_path"]):
            continue
        new_rel, digest = await asyncio.to_thread(vault.encrypt_existing, row["media_path"])
        await db.conn.execute(
            "UPDATE messages SET media_path = ?, media_sha256 = COALESCE(media_sha256, ?) WHERE id = ?",
            (new_rel, digest, row["id"]),
        )
        converted += 1
    await db.conn.commit()
    if converted:
        log.warning("Encrypted %s legacy media files", converted)
    return converted


async def backfill_evidence(db: Database, evidence: EvidenceLog, vault: Vault) -> int:
    total = 0
    while True:
        rows = await db.messages_without_evidence(500)
        if not rows:
            break
        for row in rows:
            media_sha = row["media_sha256"]
            if media_sha is None and row["media_path"] and vault.exists(row["media_path"]):
                media_sha = sha256_hex(await asyncio.to_thread(vault.read, row["media_path"]))
            entry = await evidence.append(
                EVENT_NEW,
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                raw=None,
                media_sha256=media_sha,
                media_path=row["media_path"] if media_sha else None,
                data={
                    "source": "backfill",
                    "backfilled": True,
                    "note": "сообщение сохранено версией без журнала доказательств; raw-объект недоступен",
                    "row": {field: row[field] for field in BACKFILL_FIELDS},
                },
            )
            await db.link_evidence(row["chat_id"], row["message_id"], entry.seq, entry.record_hash)
            total += 1
    if total:
        log.warning("Backfilled %s legacy messages into evidence log", total)
    return total


async def run_maintenance(db: Database, evidence: EvidenceLog, vault: Vault) -> None:
    await encrypt_legacy_media(db, vault)
    await backfill_evidence(db, evidence, vault)
