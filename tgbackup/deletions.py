from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .config import Config
from .db import Database, utcnow
from .evidence import EVENT_DELETE, EvidenceLog, observed_now
from .exporter import Exporter
from .formatting import chat_label, fmt_dt, media_label, sender_label
from .keyboards import chat_actions
from .notifier import Notifier, esc
from .settings import Settings

log = logging.getLogger(__name__)


class DeletionAggregator:
    def __init__(
        self,
        config: Config,
        db: Database,
        exporter: Exporter,
        notifier: Notifier | None,
        evidence: EvidenceLog,
        settings: Settings | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or Settings(config)
        self.db = db
        self.exporter = exporter
        self.notifier = notifier
        self.evidence = evidence
        self._pending: dict[int, list] = defaultdict(list)
        self._deleted_at: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def add(self, chat_id: int | None, message_ids: list[int], raw: dict | None = None) -> int:
        if not message_ids:
            return 0
        received_at = observed_now()
        deleted_at = utcnow()
        by_chat: dict[int, list] = defaultdict(list)
        async with self._lock:
            for row in await self.db.find_for_deletion(message_ids, chat_id):
                if self.settings.is_excluded(row["chat_id"]):
                    continue
                by_chat[row["chat_id"]].append(row)
            if not by_chat:
                return 0
            await self.db.mark_deleted([row["id"] for rows in by_chat.values() for row in rows], deleted_at)
            for resolved_chat, rows in by_chat.items():
                await self.evidence.append(
                    EVENT_DELETE,
                    chat_id=resolved_chat,
                    raw=raw,
                    observed_at=received_at,
                    data={
                        "event_chat_id": chat_id,
                        "message_ids": sorted(row["message_id"] for row in rows),
                        "prior_record_hashes": {str(row["message_id"]): row["record_hash"] for row in rows},
                    },
                )
                self._pending[resolved_chat].extend(rows)
                self._deleted_at.setdefault(resolved_chat, deleted_at)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())
        count = sum(len(rows) for rows in by_chat.values())
        log.info("Deleted: %s messages in %s chats", count, len(by_chat))
        return count

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.config.bulk_delete_window_sec)
        try:
            await self.flush()
        except Exception:
            log.exception("Deletion flush failed")

    async def flush(self) -> None:
        async with self._lock:
            pending, self._pending = self._pending, defaultdict(list)
            deleted_at, self._deleted_at = self._deleted_at, {}
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
        if self.notifier is None:
            return
        for chat_id, rows in pending.items():
            if self.settings.is_muted(chat_id):
                continue
            try:
                if len(rows) >= self.config.bulk_delete_threshold:
                    await self.send_chat_export(chat_id, len(rows))
                else:
                    await self.send_deleted(chat_id, rows, deleted_at.get(chat_id) or utcnow())
            except Exception:
                log.exception("Failed to notify deletions in chat %s", chat_id)

    async def send_chat_export(self, chat_id: int, removed: int) -> None:
        chat = await self.db.get_chat(chat_id)
        label = chat_label(chat, chat_id)
        parts = await self.exporter.export(chat_id)
        intro = (
            f"⚠️ Похоже, чат с <b>{esc(label)}</b> был удалён/очищен "
            f"(удалено {removed} сообщ.). Вот вся сохранённая история."
        )
        await self.notifier.send_export(parts, label, intro, buttons=chat_actions(chat_id))

    async def send_deleted(self, chat_id: int, rows: list, deleted_at: str) -> None:
        if not self.settings.get("notify_own_actions"):
            rows = [row for row in rows if not row["outgoing"]]
        if not rows:
            return
        chat = await self.db.get_chat(chat_id)
        tz = self.config.timezone
        for row in sorted(rows, key=lambda r: (r["date"], r["message_id"])):
            lines = [
                "🗑 <b>Сообщение удалено</b>",
                f"Чат: {esc(chat_label(chat, chat_id))}",
                f"От: {esc(sender_label(row))}",
                f"Отправлено: {fmt_dt(row['date'], tz)}",
                f"Удалено: ~{fmt_dt(deleted_at, tz)}",
            ]
            if row["edited"]:
                lines.append(f"Было изменено: {fmt_dt(row['edit_date'], tz)}")
            if row["media_type"]:
                lines.append(f"Медиа: {esc(media_label(row['media_type']))}")
            if row["media_note"]:
                lines.append(f"<i>{esc(row['media_note'])}</i>")
            if row["service_action"]:
                lines.append(esc(row["service_action"]))
            if row["fwd_from_name"] or row["fwd_from_id"]:
                lines.append(f"Переслано от: {esc(row['fwd_from_name'] or row['fwd_from_id'])}")
            lines += ["", esc(row["text"] or "—")]
            text = "\n".join(lines)
            if row["outgoing"] and row["read_at"]:
                lines.insert(4, f"Прочитано собеседником: не позднее {fmt_dt(row['read_at'], tz)}")
                text = "\n".join(lines)
            buttons = chat_actions(chat_id)
            if row["media_path"]:
                await self.notifier.send_media(row["media_path"], text, row["media_type"], buttons=buttons)
            else:
                await self.notifier.send_text(text, buttons=buttons)
