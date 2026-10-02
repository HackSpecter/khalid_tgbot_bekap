from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.custom import Message

log = logging.getLogger(__name__)

DOWNLOADABLE = {"photo", "video", "video_note", "voice", "audio", "sticker", "animation", "document"}


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


class MediaDownloader:
    def __init__(self, client: TelegramClient, media_dir: Path, max_bytes: int, concurrency: int = 3) -> None:
        self.client = client
        self.media_dir = media_dir
        self.root = media_dir.parent
        self.max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(concurrency)

    async def download(self, message: Message, chat_id: int, info: MediaInfo) -> tuple[str | None, str | None]:
        if info.ephemeral:
            return None, "самоуничтожающееся медиа не сохраняется"
        if not info.downloadable:
            return None, None
        if info.size is not None and info.size > self.max_bytes:
            return None, f"файл больше лимита ({info.size / 1024 / 1024:.1f} МБ), сохранены только метаданные"

        target_dir = self.media_dir / str(chat_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = (message.file.ext if message.file else None) or ""
        if info.type == "sticker" and not ext:
            ext = ".webp"
        suffix = info.uid.rsplit(":", 1)[-1] if info.uid else "media"
        target = target_dir / f"{message.id}_{suffix}{ext}"

        async with self._semaphore:
            try:
                result = await self.client.download_media(message, file=str(target))
            except Exception:
                log.exception("Media download failed chat=%s msg=%s", chat_id, message.id)
                return None, "не удалось скачать медиа"
        if not result:
            return None, "не удалось скачать медиа"
        return Path(result).resolve().relative_to(self.root).as_posix(), None
