from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import from_iso
from .notifier import esc

QUOTE_LIMIT = 2800
COLLAPSE_CHARS = 350
COLLAPSE_LINES = 6

MEDIA_ICONS = {
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "video_note": "⏺ Кружок",
    "voice": "🎤 Голосовое",
    "audio": "🎵 Аудио",
    "sticker": "🙂 Стикер",
    "animation": "🎞 GIF",
    "document": "📎 Файл",
    "webpage": "🔗 Ссылка",
    "contact": "👤 Контакт",
    "geo": "📍 Локация",
    "geo_live": "📍 Live-локация",
    "venue": "📍 Место",
    "poll": "📊 Опрос",
    "dice": "🎲 Кубик",
    "story": "📖 История",
}


def quote(text: str | None) -> str:
    if not text:
        return "<blockquote><i>без текста</i></blockquote>"
    body = text if len(text) <= QUOTE_LIMIT else text[: QUOTE_LIMIT - 1] + "…"
    collapsed = len(body) > COLLAPSE_CHARS or body.count("\n") >= COLLAPSE_LINES
    tag = "<blockquote expandable>" if collapsed else "<blockquote>"
    return f"{tag}{esc(body)}</blockquote>"


def person(sender_id: int | None, name: str | None, username: str | None, outgoing: bool = False) -> str:
    if outgoing:
        return "👤 <b>Вы</b>"
    title = esc(name or (str(sender_id) if sender_id else "неизвестно"))
    link = f'<a href="tg://user?id={sender_id}">{title}</a>' if sender_id and sender_id > 0 else f"<b>{title}</b>"
    return f"👤 {link}" + (f" · @{esc(username)}" if username else "")


def chat_line(chat: Mapping | None, chat_id: int) -> str | None:
    if chat is None or chat["type"] == "private":
        return None
    title = esc(chat["title"] or chat_id)
    return f"💬 <b>{title}</b>" + (f" · @{esc(chat['username'])}" if chat["username"] else "")


def moment(value: str | datetime | None, tz: ZoneInfo, reference: datetime | None = None) -> str:
    dt = from_iso(value) if isinstance(value, str) else value
    if dt is None:
        return "—"
    local = dt.astimezone(tz)
    reference = (reference or datetime.now(tz)).astimezone(tz)
    return local.strftime("%H:%M:%S") if local.date() == reference.date() else local.strftime("%d.%m.%Y %H:%M:%S")


def media_line(row: Mapping) -> str | None:
    media_type = row["media_type"]
    if not media_type or media_type == "service":
        return None
    label = MEDIA_ICONS.get(media_type, f"📎 {media_type}")
    note = row["media_note"]
    return f"{label}" + (f" · <i>{esc(note)}</i>" if note else "")


def deleted_card(row: Mapping, chat: Mapping | None, deleted_at: str, tz: ZoneInfo) -> str:
    lines = ["🗑 <b>Удалено сообщение</b>", person(row["sender_id"], row["sender_name"], row["sender_username"], bool(row["outgoing"]))]
    group = chat_line(chat, row["chat_id"])
    if group:
        lines.append(group)
    if row["fwd_from_name"] or row["fwd_from_id"]:
        lines.append(f"↪️ Переслано от {esc(row['fwd_from_name'] or row['fwd_from_id'])}")
    media = media_line(row)
    if media:
        lines.append(media)
    if row["service_action"]:
        lines.append(f"⚙️ {esc(row['service_action'])}")
    if row["text"] or not (media or row["service_action"]):
        lines.append(quote(row["text"]))

    times = [f"отправлено {moment(row['date'], tz)}", f"удалено {moment(deleted_at, tz)}"]
    if row["edited"]:
        times.insert(1, f"изменялось {moment(row['edit_date'], tz)}")
    if row["outgoing"] and row["read_at"]:
        times.append(f"прочитано ✓✓ {moment(row['read_at'], tz)}")
    lines.append(f"<i>🕐 {' · '.join(times)}</i>")
    return "\n".join(lines)


def edited_card(old: Mapping, new_text: str | None, edit_date: str | None, media_change: tuple[str, str] | None,
                chat: Mapping | None, tz: ZoneInfo) -> str:
    lines = ["✏️ <b>Сообщение изменено</b>", person(old["sender_id"], old["sender_name"], old["sender_username"], bool(old["outgoing"]))]
    group = chat_line(chat, old["chat_id"])
    if group:
        lines.append(group)
    if media_change:
        lines.append(f"📎 Медиа: {esc(media_change[0])} → {esc(media_change[1])}")
    lines += ["<b>Было</b>", quote(old["text"]), "<b>Стало</b>", quote(new_text)]
    lines.append(f"<i>🕐 отправлено {moment(old['date'], tz)} · изменено {moment(edit_date, tz)}</i>")
    return "\n".join(lines)


def view_once_card(record, stored, chat: Mapping | None, tz: ZoneInfo) -> str:
    label = MEDIA_ICONS.get(record.media_type, f"📎 {record.media_type}")
    lines = [
        "🔥 <b>View-once перехвачено</b>",
        person(record.sender_id, record.sender_name, record.sender_username, record.outgoing),
    ]
    group = chat_line(chat, record.chat_id)
    if group:
        lines.append(group)
    lines.append(f"Тип: {label}")
    if stored and stored.path:
        lines.append(f"Сохранено: <code>data/{esc(stored.path)}</code>")
    else:
        lines.append(f"⚠️ {esc((stored.note if stored else None) or 'не удалось сохранить')}")
    lines.append(f"<i>🕐 {moment(record.date, tz)}</i>")
    return "\n".join(lines)


def profile_card(user_id: int, name: str, changes: list[tuple[str, str | None, str | None]]) -> str:
    lines = ["🪪 <b>Профиль изменён</b>", f'👤 <a href="tg://user?id={user_id}">{esc(name)}</a> · <code>{user_id}</code>', ""]
    for label, old, new in changes:
        if old is None and new is None:
            continue
        before = f"<s>{esc(old)}</s>" if old else "<i>пусто</i>"
        after = f"<b>{esc(new)}</b>" if new else "<i>пусто</i>"
        lines.append(f"• {label}: {before} → {after}")
    return "\n".join(lines)
