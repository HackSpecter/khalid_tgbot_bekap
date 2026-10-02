from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import from_iso

MEDIA_LABELS = {
    "photo": "фото",
    "video": "видео",
    "video_note": "кружок",
    "voice": "голосовое",
    "audio": "аудио",
    "sticker": "стикер",
    "animation": "GIF",
    "document": "файл",
    "webpage": "ссылка",
    "contact": "контакт",
    "geo": "локация",
    "geo_live": "live-локация",
    "venue": "место",
    "poll": "опрос",
    "dice": "кубик",
    "story": "история",
}


def fmt_dt(value: str | datetime | None, tz: ZoneInfo, pattern: str = "%d.%m.%Y %H:%M:%S") -> str:
    if isinstance(value, str):
        value = from_iso(value)
    if value is None:
        return "—"
    return value.astimezone(tz).strftime(pattern)


def full_name(first: str | None, last: str | None) -> str:
    return " ".join(part for part in (first, last) if part).strip()


def sender_label(row: Mapping) -> str:
    name = row["sender_name"] or (str(row["sender_id"]) if row["sender_id"] else "неизвестно")
    username = row["sender_username"]
    return f"{name} (@{username})" if username else name


def chat_label(chat: Mapping | None, chat_id: int) -> str:
    if chat is None:
        return str(chat_id)
    title = chat["title"] or str(chat_id)
    return f"{title} (@{chat['username']})" if chat["username"] else title


def media_label(media_type: str | None) -> str:
    if not media_type:
        return ""
    return MEDIA_LABELS.get(media_type, media_type)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"
