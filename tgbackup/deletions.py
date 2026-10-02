from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path

from .config import Config
from .db import Database, utcnow
from .exporter import Exporter
from .formatting import chat_label, fmt_dt, media_label, sender_label
from .notifier import CAPTION_LIMIT, Notifier, esc

log = logging.getLogger(__name__)

NO_CAPTION_TYPES = {"sticker", "video_note"}


class DeletionAggregator:
    def __init__(self, config: Config, db: Database, exporter: Exporter, notifier: Notifier | None) -> None:
        self.config = config
        self.db = db
        self.exporter = exporter
        self.notifier = notifier
        self._pending: list[tuple[int | None, list[int]]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def add(self, chat_id: int | None, message_ids: list[int]) -> None:
        if not message_ids:
            return
        async with self._lock:
            self._pending.append((chat_id, message_ids))
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.config.bulk_delete_window_sec)
        try:
            await self.flush()
        except Exception:
            log.exception("Deletion flush failed")

    async def flush(self) -> None:
        async with self._lock:
            pending, self._pending = self._pending, []
            if self._flush_task is asyncio.current_task():
                self._flush_task = None
        if not pending:
            return

        deleted_at = utcnow()
        by_chat: dict[int, list] = defaultdict(list)
        seen: set[int] = set()
        for chat_id, ids in pending:
            for row in await self.db.find_for_deletion(ids, chat_id):
                if row["id"] in seen or row["chat_id"] in self.config.excluded_chat_ids:
                    continue
                seen.add(row["id"])
                by_chat[row["chat_id"]].append(row)

        await self.db.mark_deleted(seen, deleted_at)
        log.info("Deleted: %s messages in %s chats", len(seen), len(by_chat))

        if self.notifier is None:
            return
        for chat_id, rows in by_chat.items():
            try:
                if len(rows) >= self.config.bulk_delete_threshold:
                    await self.send_chat_export(chat_id, len(rows))
                else:
                    await self.send_deleted(chat_id, rows, deleted_at)
            except Exception:
                log.exception("Failed to notify deletions in chat %s", chat_id)

    async def send_chat_export(self, chat_id: int, removed: int) -> None:
        chat = await self.db.get_chat(chat_id)
        label = esc(chat_label(chat, chat_id))
        parts = await self.exporter.export(chat_id)
        total = len(parts)
        for index, part in enumerate(parts, 1):
            suffix = f" (часть {index}/{total})" if total > 1 else ""
            caption = (
                f"⚠️ Похоже, чат с <b>{label}</b> был удалён/очищен "
                f"(удалено {removed} сообщ.). Вот вся сохранённая история{suffix}."
            )
            await self.notifier.send_file(part, caption, as_document=True)

    async def send_deleted(self, chat_id: int, rows: list, deleted_at: str) -> None:
        if not self.config.notify_own_actions:
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
            lines += ["", esc(row["text"] or "—")]
            text = "\n".join(lines)

            media = self.config.data_dir / row["media_path"] if row["media_path"] else None
            if media is not None and Path(media).is_file():
                if row["media_type"] in NO_CAPTION_TYPES or len(text) > CAPTION_LIMIT:
                    await self.notifier.send_text(text)
                    await self.notifier.send_file(media)
                else:
                    await self.notifier.send_file(media, text, as_document=row["media_type"] == "document")
            else:
                await self.notifier.send_text(text)
