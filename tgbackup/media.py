from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.custom import Message

from .crypto import Vault

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class MediaInfo:
    type: str
    uid: str | None
    size: int | None
    downloadable: bool
    ephemeral: bool


def _document_kind(message: Message) -> str:
    if message.sticker:
        return "sticker"
    if message.video_note:
        return "video_note"
    if message.voice:
        return "voice"
    if message.gif:
        return "animation"
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    return "document"


def inspect(message: Message) -> MediaInfo | None:
    media = message.media
    if media is None:
        return None

    ephemeral = bool(getattr(media, "ttl_seconds", None))

    if isinstance(media, types.MessageMediaPhoto):
        photo = media.photo
        return MediaInfo(
            type="photo",
            uid=f"photo:{photo.id}" if photo else None,
            size=message.file.size if photo and message.file else None,
            downloadable=photo is not None,
            ephemeral=ephemeral,
        )

    if isinstance(media, types.MessageMediaDocument):
        document = media.document
        if document is None:
            return MediaInfo("document", None, None, False, ephemeral)
        return MediaInfo(
            type=_document_kind(message),
            uid=f"doc:{document.id}",
            size=document.size,
            downloadable=True,
            ephemeral=ephemeral,
        )

    simple = {
        types.MessageMediaWebPage: "webpage",
        types.MessageMediaContact: "contact",
        types.MessageMediaGeo: "geo",
        types.MessageMediaGeoLive: "geo_live",
        types.MessageMediaVenue: "venue",
        types.MessageMediaPoll: "poll",
        types.MessageMediaDice: "dice",
        types.MessageMediaGame: "game",
        types.MessageMediaInvoice: "invoice",
        types.MessageMediaStory: "story",
    }
    for cls, name in simple.items():
        if isinstance(media, cls):
            return MediaInfo(name, None, None, False, ephemeral)
    return MediaInfo(type(media).__name__.removeprefix("MessageMedia").lower() or "unknown", None, None, False, ephemeral)


def describe(message: Message, info: MediaInfo) -> str | None:
    media = message.media
    if info.type == "contact":
        name = " ".join(filter(None, [media.first_name, media.last_name]))
        return f"Контакт: {name} {media.phone_number}".strip()
    if info.type in {"geo", "geo_live", "venue"}:
        geo = getattr(media, "geo", None)
        if isinstance(geo, types.GeoPoint):
            title = getattr(media, "title", "") or ""
            return f"Локация: {title} {geo.lat:.6f}, {geo.long:.6f}".strip()
    if info.type == "poll":
        poll = media.poll
        question = getattr(poll.question, "text", poll.question)
        answers = "; ".join(getattr(a.text, "text", a.text) for a in poll.answers)
        return f"Опрос: {question} [{answers}]"
    if info.type == "dice":
        return f"Кубик {media.emoticon}: {media.value}"
    if info.type == "webpage":
        page = media.webpage
        url = getattr(page, "url", None)
        return f"Ссылка: {url}" if url else None
    return None


@dataclass(frozen=True)
class StoredMedia:
    path: str | None
    note: str | None
    sha256: str | None = None
    size: int | None = None


class MediaDownloader:
    def __init__(self, client: TelegramClient, vault: Vault, max_bytes: int, concurrency: int = 3) -> None:
        self.client = client
        self.vault = vault
        self.max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(concurrency)

    async def download(self, message: Message, chat_id: int, info: MediaInfo) -> StoredMedia:
        if info.ephemeral:
            return StoredMedia(None, "самоуничтожающееся медиа не сохраняется")
        if not info.downloadable:
            return StoredMedia(None, None)
        if info.size is not None and info.size > self.max_bytes:
            return StoredMedia(
                None, f"файл больше лимита ({info.size / 1024 / 1024:.1f} МБ), сохранены только метаданные"
            )

        ext = (message.file.ext if message.file else None) or ""
        if info.type == "sticker" and not ext:
            ext = ".webp"
        suffix = info.uid.rsplit(":", 1)[-1] if info.uid else "media"
        rel = f"media/{chat_id}/{message.id}_{suffix}{ext}"

        async with self._semaphore:
            try:
                data = await self.client.download_media(message, file=bytes)
            except Exception:
                log.exception("Media download failed chat=%s msg=%s", chat_id, message.id)
                return StoredMedia(None, "не удалось скачать медиа")
        if not data:
            return StoredMedia(None, "не удалось скачать медиа")
        stored, digest, size = await asyncio.to_thread(self.vault.write, rel, data)
        return StoredMedia(stored, None, digest, size)

    async def download_profile_photo(self, user, photo_id: int) -> StoredMedia:
        async with self._semaphore:
            try:
                data = await self.client.download_profile_photo(user, file=bytes, download_big=True)
            except Exception:
                log.exception("Profile photo download failed user=%s", getattr(user, "id", None))
                return StoredMedia(None, "не удалось скачать фото профиля")
        if not data:
            return StoredMedia(None, None)
        rel = f"media/profiles/{user.id}/{photo_id}.jpg"
        stored, digest, size = await asyncio.to_thread(self.vault.write, rel, data)
        return StoredMedia(stored, None, digest, size)
