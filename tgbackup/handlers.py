from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient, events, utils
from telethon.tl import types

from .config import Config
from .db import Database, MessageRecord, to_iso
from .evidence import EVENT_EDIT, EVENT_NEW, EVENT_REACTION, EVENT_READ, EvidenceLog, observed_now, raw_dict
from .extract import describe_action, forward_info, reactions_summary
from .formatting import chat_label, fmt_dt, full_name, media_label, sender_label
from .media import MediaDownloader, StoredMedia, describe, inspect
from .keyboards import chat_actions
from .notifier import Notifier, esc
from .settings import Settings

if TYPE_CHECKING:
    from .deletions import DeletionAggregator
    from .profiles import ProfileTracker

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
        evidence: EvidenceLog,
        notifier: Notifier | None = None,
        deletions: "DeletionAggregator | None" = None,
        profiles: "ProfileTracker | None" = None,
        settings: Settings | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or Settings(config)
        self.db = db
        self.downloader = downloader
        self.evidence = evidence
        self.notifier = notifier
        self.deletions = deletions
        self.profiles = profiles
        self.client: TelegramClient | None = None
        self.me_id: int | None = None

    def register(self, client: TelegramClient) -> None:
        self.client = client
        client.add_event_handler(self.on_new, events.NewMessage())
        client.add_event_handler(self.on_edit, events.MessageEdited())
        client.add_event_handler(self.on_delete, events.MessageDeleted())
        client.add_event_handler(
            self.on_service, events.Raw(types=[types.UpdateNewMessage, types.UpdateNewChannelMessage])
        )
        client.add_event_handler(self.on_reactions, events.Raw(types=types.UpdateMessageReactions))
        client.add_event_handler(self.on_read, events.MessageRead(inbox=False))

    def is_ignored_chat(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return False
        return (
            self.settings.is_excluded(chat_id)
            or chat_id == self.me_id
            or chat_id == self.config.notifier_bot_id
        )

    def kind_enabled(self, kind: str) -> bool:
        if kind == "private":
            return self.settings.get("track_private")
        if kind in {"group", "supergroup"}:
            return self.settings.get("track_groups")
        if kind == "channel":
            return self.settings.get("track_channels")
        return False

    def message_kind(self, message) -> str:
        if message.is_private:
            return "private"
        if message.is_group:
            return "group"
        return "channel"

    def should_track(self, message) -> bool:
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
        if self.profiles is not None:
            try:
                await self.profiles.observe(user)
            except Exception:
                log.exception("Profile observe failed for %s", user.id)

    async def resolve_sender(self, message):
        sender = getattr(message, "sender", None)
        if sender is None and message.sender_id is not None:
            try:
                sender = await message.get_sender()
            except Exception:
                log.debug("Cannot resolve sender %s", message.sender_id, exc_info=True)
        return sender

    async def build_record(self, message) -> MessageRecord:
        sender = await self.resolve_sender(message)
        if isinstance(sender, types.User):
            await self.remember_user(sender)

        service = isinstance(message, types.MessageService)
        info = None if service else inspect(message)
        note = None
        if info is not None:
            note = "самоуничтожающееся медиа не сохраняется" if info.ephemeral else describe(message, info)
        fwd_id, fwd_name, fwd_date = (None, None, None) if service else forward_info(message)
        reply_to = message.reply_to.reply_to_msg_id if isinstance(message.reply_to, types.MessageReplyHeader) else None

        return MessageRecord(
            chat_id=message.chat_id,
            message_id=message.id,
            date=to_iso(message.date),
            sender_id=message.sender_id,
            sender_name=entity_title(sender) if sender is not None else None,
            sender_username=getattr(sender, "username", None),
            outgoing=bool(message.out),
            text=None if service else (message.raw_text or None),
            reply_to=reply_to,
            grouped_id=None if service else message.grouped_id,
            media_type="service" if service else (info.type if info else None),
            media_uid=info.uid if info else None,
            media_size=info.size if info else None,
            media_note=note,
            edited=not service and message.edit_date is not None,
            edit_date=None if service else to_iso(message.edit_date),
            fwd_from_id=fwd_id,
            fwd_from_name=fwd_name,
            fwd_date=fwd_date,
            reactions=reactions_summary(getattr(message, "reactions", None)),
            service_action=describe_action(message.action, bool(message.out), reply_to) if service else None,
        )

    async def fetch_media(self, message, record: MessageRecord) -> StoredMedia | None:
        info = None if isinstance(message, types.MessageService) else inspect(message)
        if info is None or not (info.downloadable or info.ephemeral):
            return None
        stored = await self.downloader.download(message, record.chat_id, info)
        await self.db.set_media(
            record.chat_id, record.message_id, stored.path, stored.size or info.size, stored.note or record.media_note,
            stored.sha256, stored.thumb_path,
        )
        return stored

    async def store(
        self, message, *, chat: object | None = None, with_media: bool = True, source: str = "live"
    ) -> MessageRecord | None:
        if not isinstance(message, (types.Message, types.MessageService)):
            return None
        if chat is None:
            chat = getattr(message, "chat", None)
            if chat is None:
                try:
                    chat = await message.get_chat()
                except Exception:
                    log.debug("Cannot resolve chat %s", message.chat_id, exc_info=True)
        await self.remember_chat(message.chat_id, chat)

        existing = await self.db.get_message(message.chat_id, message.id)
        if existing is not None and existing["first_seq"] is not None:
            if isinstance(message, types.Message):
                await self.apply_edit(existing, message, notify=False, with_media=with_media)
            return None

        record = await self.build_record(message)
        await self.db.save_message(record)
        stored = None
        if with_media and record.media_uid:
            stored = await self.fetch_media(message, record)

        entry = await self.evidence.append(
            EVENT_NEW,
            chat_id=record.chat_id,
            message_id=record.message_id,
            raw=raw_dict(message),
            media_sha256=stored.sha256 if stored else None,
            media_path=stored.path if stored else None,
            data={"source": source, "media_note": (stored.note if stored else None) or record.media_note},
        )
        await self.db.link_evidence(record.chat_id, record.message_id, entry.seq, entry.record_hash)
        return record

    async def apply_edit(self, old, message, *, notify: bool = True, with_media: bool = True) -> None:
        record = await self.build_record(message)
        text_changed = (old["text"] or "") != (record.text or "")
        media_changed = old["media_uid"] != record.media_uid
        reactions_changed = (old["reactions"] or None) != record.reactions

        if not text_changed and not media_changed:
            if reactions_changed:
                await self.log_reactions(record.chat_id, record.message_id, message.reactions, record.reactions)
            return

        stored = await self.fetch_media(message, record) if media_changed and with_media else None
        record.edited = True
        record.edit_date = record.edit_date or to_iso(message.date)
        entry = await self.evidence.append(
            EVENT_EDIT,
            chat_id=record.chat_id,
            message_id=record.message_id,
            raw=raw_dict(message),
            media_sha256=stored.sha256 if stored else None,
            media_path=stored.path if stored else None,
            data={
                "previous_record_hash": old["record_hash"],
                "previous_text": old["text"],
                "previous_media_sha256": old["media_sha256"],
                "text_changed": text_changed,
                "media_changed": media_changed,
            },
        )
        await self.db.record_edit(old, record, entry.seq)
        await self.db.save_message(record)
        if media_changed:
            await self.db.set_media(
                record.chat_id,
                record.message_id,
                stored.path if stored else None,
                stored.size if stored else record.media_size,
                (stored.note if stored else None) or record.media_note,
                stored.sha256 if stored else None,
                stored.thumb_path if stored else None,
            )
        if notify and self.should_notify(record.chat_id, record.outgoing):
            await self.notify_edit(old, record, media_changed)

    def should_notify(self, chat_id: int, outgoing: bool = False) -> bool:
        if self.notifier is None or self.settings.is_muted(chat_id):
            return False
        return not outgoing or self.settings.get("notify_own_actions")

    async def apply_read(self, chat_id: int, max_id: int, *, raw=None, source: str = "live") -> int:
        message_ids = await self.db.unread_outgoing(chat_id, max_id)
        if not message_ids:
            return 0
        observed_at = observed_now()
        entry = await self.evidence.append(
            EVENT_READ,
            chat_id=chat_id,
            raw=raw,
            observed_at=observed_at,
            data={
                "max_id": max_id,
                "message_ids": message_ids,
                "source": source,
                "meaning": "собеседник прочитал исходящие сообщения не позднее observed_at",
            },
        )
        await self.db.mark_read(chat_id, message_ids, observed_at, entry.seq)
        return len(message_ids)

    async def on_read(self, event: events.MessageRead.Event) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None or self.is_ignored_chat(chat_id):
                return
            await self.apply_read(chat_id, event.max_id, raw=raw_dict(event.original_update))
        except Exception:
            log.exception("Failed to process read receipt")

    async def log_reactions(self, chat_id: int, message_id: int, reactions, summary: str | None) -> None:
        await self.evidence.append(
            EVENT_REACTION,
            chat_id=chat_id,
            message_id=message_id,
            raw=raw_dict(reactions),
            data={"summary": summary},
        )
        await self.db.set_reactions(chat_id, message_id, summary)

    async def on_new(self, event: events.NewMessage.Event) -> None:
        try:
            if not self.should_track(event.message):
                return
            await self.store(event.message)
        except Exception:
            log.exception("Failed to store message chat=%s msg=%s", event.chat_id, event.id)

    async def on_service(self, update) -> None:
        message = update.message
        if not isinstance(message, types.MessageService):
            return
        try:
            message._finish_init(self.client, {}, None)
            if not self.should_track(message):
                return
            await self.store(message)
        except Exception:
            log.exception("Failed to store service message %s", getattr(message, "id", None))

    async def on_reactions(self, update: types.UpdateMessageReactions) -> None:
        try:
            chat_id = utils.get_peer_id(update.peer)
            if self.is_ignored_chat(chat_id):
                return
            row = await self.db.get_message(chat_id, update.msg_id)
            if row is None:
                return
            summary = reactions_summary(update.reactions)
            if (row["reactions"] or None) != summary:
                await self.log_reactions(chat_id, update.msg_id, update.reactions, summary)
        except Exception:
            log.exception("Failed to process reactions update")

    async def on_edit(self, event: events.MessageEdited.Event) -> None:
        message = event.message
        try:
            if not self.should_track(message):
                return
            old = await self.db.get_message(message.chat_id, message.id)
            if old is None or old["first_seq"] is None:
                await self.store(message)
                return
            await self.apply_edit(old, message)
        except Exception:
            log.exception("Failed to process edit chat=%s msg=%s", event.chat_id, message.id)

    async def on_delete(self, event: events.MessageDeleted.Event) -> None:
        try:
            if self.deletions is None:
                return
            chat_id = event.chat_id
            if chat_id is not None and self.is_ignored_chat(chat_id):
                return
            await self.deletions.add(chat_id, list(event.deleted_ids), raw_dict(event.original_update))
        except Exception:
            log.exception("Failed to process deletion %s", getattr(event, "deleted_ids", None))

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
        await self.notifier.send_text("\n".join(lines), buttons=chat_actions(record.chat_id))
