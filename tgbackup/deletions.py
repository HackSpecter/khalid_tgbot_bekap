from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .config import Config
from .db import Database, utcnow
from .evidence import EVENT_DELETE, EvidenceLog, observed_now
from .exporter import Exporter
from .cards import deleted_card
from .formatting import chat_label
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
        self._pending: dict[int, list[tuple]] = defaultdict(list)
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
                self._pending[resolved_chat].extend((row, deleted_at) for row in rows)
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
                    await self.send_deleted(chat_id, rows)
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

    async def send_deleted(self, chat_id: int, items: list[tuple]) -> None:
        if not self.settings.get("notify_own_actions"):
            items = [(row, at) for row, at in items if not row["outgoing"]]
        if not items:
            return
        chat = await self.db.get_chat(chat_id)
        buttons = chat_actions(chat_id)
        for row, deleted_at in sorted(items, key=lambda item: (item[0]["date"], item[0]["message_id"])):
            text = deleted_card(row, chat, deleted_at, self.config.timezone)
            if row["media_path"]:
                await self.notifier.send_media(row["media_path"], text, row["media_type"], buttons=buttons)
            else:
                await self.notifier.send_text(text, buttons=buttons)
