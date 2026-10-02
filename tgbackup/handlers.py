from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient, events
from telethon.tl import types
from telethon.tl.custom import Message

from .config import Config
from .db import Database, MessageRecord, to_iso
from .formatting import chat_label, fmt_dt, full_name, media_label, sender_label
from .media import MediaDownloader, describe, inspect
from .notifier import Notifier, esc

if TYPE_CHECKING:
    from .deletions import DeletionAggregator

log = logging.getLogger(__name__)


def entity_title(entity: object) -> str | None:
    if isinstance(entity, types.User):
        if entity.deleted:
            return "Удалённый аккаунт"
        return full_name(entity.first_name, entity.last_name) or entity.username or str(entity.id)
    return getattr(entity, "title", None)


def entity_type(entity: object) -> str:
    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    return "unknown"


class Tracker:
    def __init__(
        self,
        config: Config,
        db: Database,
        downloader: MediaDownloader,
        notifier: Notifier | None = None,
        deletions: "DeletionAggregator | None" = None,
    ) -> None:
        self.config = config
        self.db = db
        self.downloader = downloader
        self.notifier = notifier
        self.deletions = deletions
        self.me_id: int | None = None

    def register(self, client: TelegramClient) -> None:
        client.add_event_handler(self.on_new, events.NewMessage())
        client.add_event_handler(self.on_edit, events.MessageEdited())
        client.add_event_handler(self.on_delete, events.MessageDeleted())

    def is_ignored_chat(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return False
        return (
            chat_id in self.config.excluded_chat_ids
            or chat_id == self.me_id
            or chat_id == self.config.notifier_bot_id
        )

    def kind_enabled(self, kind: str) -> bool:
        if kind == "private":
            return self.config.track_private
        if kind in {"group", "supergroup"}:
            return self.config.track_groups
        if kind == "channel":
            return self.config.track_channels
        return False

    def message_kind(self, message: Message) -> str:
        if message.is_private:
            return "private"
        if message.is_group:
            return "group"
        return "channel"

    def should_track(self, message: Message) -> bool:
        return not self.is_ignored_chat(message.chat_id) and self.kind_enabled(self.message_kind(message))

    async def remember_chat(self, chat_id: int, entity: object | None) -> None:
        if entity is None:
            if await self.db.get_chat(chat_id) is None:
                await self.db.upsert_chat(chat_id, None, "unknown", None)
            return
        await self.db.upsert_chat(chat_id, entity_title(entity), entity_type(entity), getattr(entity, "username", None))
        if isinstance(entity, types.User):
            await self.remember_user(entity)

    async def remember_user(self, user: types.User) -> None:
        await self.db.upsert_user(user.id, user.first_name, user.last_name, user.username, user.phone, bool(user.bot))

    async def build_record(self, message: Message) -> MessageRecord:
        sender = message.sender
        if sender is None and message.sender_id is not None:
            try:
                sender = await message.get_sender()
            except Exception:
                log.debug("Cannot resolve sender %s", message.sender_id, exc_info=True)
        if isinstance(sender, types.User):
            await self.remember_user(sender)

        info = inspect(message)
        note = None
        if info is not None:
            note = "самоуничтожающееся медиа не сохраняется" if info.ephemeral else describe(message, info)

        return MessageRecord(
            chat_id=message.chat_id,
            message_id=message.id,
            date=to_iso(message.date),
            sender_id=message.sender_id,
            sender_name=entity_title(sender) if sender is not None else None,
            sender_username=getattr(sender, "username", None),
            outgoing=bool(message.out),
            text=message.raw_text or None,
            reply_to=message.reply_to_msg_id,
            grouped_id=message.grouped_id,
            media_type=info.type if info else None,
            media_uid=info.uid if info else None,
            media_size=info.size if info else None,
            media_note=note,
            edited=message.edit_date is not None,
            edit_date=to_iso(message.edit_date),
        )

    async def fetch_media(self, message: Message, record: MessageRecord, *, replace: bool = False) -> None:
        info = inspect(message)
        if info is None or not (info.downloadable or info.ephemeral):
            if replace:
                await self.db.set_media(record.chat_id, record.message_id, None, None, record.media_note)
            return
        path, note = await self.downloader.download(message, record.chat_id, info)
        if path or note or replace:
            await self.db.set_media(record.chat_id, record.message_id, path, info.size, note or record.media_note)

    async def store(self, message: Message, *, chat: object | None = None, with_media: bool = True) -> MessageRecord | None:
        if not isinstance(message, types.Message):
            return None
        if chat is None:
            chat = message.chat
            if chat is None:
                try:
                    chat = await message.get_chat()
                except Exception:
                    log.debug("Cannot resolve chat %s", message.chat_id, exc_info=True)
        await self.remember_chat(message.chat_id, chat)

        existing = await self.db.get_message(message.chat_id, message.id)
        record = await self.build_record(message)
        await self.db.save_message(record)
        needs_media = existing is None or (record.media_uid and not existing["media_path"])
        if with_media and record.media_type and needs_media:
            await self.fetch_media(message, record)
        return record

    async def on_new(self, event: events.NewMessage.Event) -> None:
        try:
            if not self.should_track(event.message):
                return
            await self.store(event.message)
        except Exception:
            log.exception("Failed to store message chat=%s msg=%s", event.chat_id, event.id)

    async def on_edit(self, event: events.MessageEdited.Event) -> None:
        message = event.message
        try:
            if not self.should_track(message):
                return
            old = await self.db.get_message(message.chat_id, message.id)
            if old is None:
                await self.store(message)
                return

            record = await self.build_record(message)
            text_changed = (old["text"] or "") != (record.text or "")
            media_changed = old["media_uid"] != record.media_uid
            if not text_changed and not media_changed:
                return

            record.edited = True
            record.edit_date = record.edit_date or to_iso(message.date)
            await self.db.record_edit(old, record)
            await self.db.save_message(record)
            if media_changed:
                await self.fetch_media(message, record, replace=True)
            if self.notifier and (not record.outgoing or self.config.notify_own_actions):
                await self.notify_edit(old, record, media_changed)
        except Exception:
            log.exception("Failed to process edit chat=%s msg=%s", event.chat_id, message.id)

    async def on_delete(self, event: events.MessageDeleted.Event) -> None:
        try:
            if self.deletions is None:
                return
            chat_id = event.chat_id
            if chat_id is not None and self.is_ignored_chat(chat_id):
                return
            await self.deletions.add(chat_id, list(event.deleted_ids))
        except Exception:
            log.exception("Failed to queue deletion %s", getattr(event, "deleted_ids", None))

    async def notify_edit(self, old, record: MessageRecord, media_changed: bool) -> None:
        chat = await self.db.get_chat(record.chat_id)
        tz = self.config.timezone
        lines = [
            "✏️ <b>Сообщение изменено</b>",
            f"Чат: {esc(chat_label(chat, record.chat_id))}",
            f"От: {esc(sender_label(old))}",
            f"Отправлено: {fmt_dt(old['date'], tz)}",
            f"Изменено: {fmt_dt(record.edit_date, tz)}",
        ]
        if media_changed:
            lines.append(
                f"Медиа: {esc(media_label(old['media_type']) or 'нет')} → {esc(media_label(record.media_type) or 'нет')}"
            )
        lines += ["", "<b>Было:</b>", esc(old["text"] or "—"), "", "<b>Стало:</b>", esc(record.text or "—")]
        await self.notifier.send_text("\n".join(lines))
