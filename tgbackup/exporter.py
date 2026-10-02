from __future__ import annotations

import asyncio
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .config import Config
from .db import Database, from_iso
from .formatting import fmt_dt, human_size, media_label, sender_label

PALETTE = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
STORED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".oga", ".ogg", ".mp3", ".m4a", ".zip", ".gz"}


@dataclass
class MediaView:
    type: str
    label: str
    href: str | None = None
    source: Path | None = None
    size: int = 0
    name: str | None = None
    note: str | None = None

    @property
    def size_label(self) -> str:
        return human_size(self.size) if self.size else ""

    @property
    def ext(self) -> str:
        return Path(self.href).suffix.lower() if self.href else ""


@dataclass
class ReplyView:
    sender: str
    text: str
    anchor: str


@dataclass
class EditView:
    at: str
    text: str | None


@dataclass
class MessageView:
    message_id: int
    outgoing: bool
    sender: str
    color: str
    time: str
    full_time: str
    day: str
    text: str | None
    reply: ReplyView | None
    media: MediaView | None
    edited_at: str | None
    deleted_at: str | None
    edits: list[EditView] = field(default_factory=list)
    new_day: bool = False
    show_sender: bool = True


def slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return slug[:40] or fallback


def day_title(value: datetime) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def snippet(row, limit: int = 90) -> str:
    text = (row["text"] or "").replace("\n", " ").strip()
    if not text and row["media_type"]:
        text = media_label(row["media_type"]).capitalize()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Exporter:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.env = Environment(
            loader=PackageLoader("tgbackup", "templates"),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    async def export(self, chat_id: int) -> list[Path]:
        chat = await self.db.get_chat(chat_id)
        rows = await self.db.chat_messages(chat_id)
        edits = await self.db.chat_edits(chat_id)
        views = self.build_views(chat, rows, edits)
        return await asyncio.to_thread(self.write_parts, chat_id, chat, views)

    def build_views(self, chat, rows, edits) -> list[MessageView]:
        tz = self.config.timezone
        by_id = {row["message_id"]: row for row in rows}
        is_private = chat is not None and chat["type"] == "private"
        views: list[MessageView] = []
        previous_day = None
        previous_sender = None

        for row in rows:
            sent = from_iso(row["date"]).astimezone(tz)
            day = sent.date()
            reply = None
            if row["reply_to"] and row["reply_to"] in by_id:
                target = by_id[row["reply_to"]]
                reply = ReplyView(sender_label(target), snippet(target), f"m{target['message_id']}")
            elif row["reply_to"]:
                reply = ReplyView("", "Сообщение не сохранено", f"m{row['reply_to']}")

            sender_key = (row["outgoing"], row["sender_id"])
            view = MessageView(
                message_id=row["message_id"],
                outgoing=bool(row["outgoing"]),
                sender=sender_label(row),
                color=PALETTE[(row["sender_id"] or 0) % len(PALETTE)],
                time=sent.strftime("%H:%M"),
                full_time=fmt_dt(sent, tz),
                day=day_title(sent),
                text=row["text"],
                reply=reply,
                media=self.media_view(row),
                edited_at=fmt_dt(row["edit_date"], tz) if row["edited"] else None,
                deleted_at=fmt_dt(row["deleted_at"], tz) if row["deleted"] else None,
                edits=[EditView(fmt_dt(e["edited_at"], tz), e["old_text"]) for e in edits.get(row["message_id"], [])],
                new_day=day != previous_day,
                show_sender=not is_private and (day != previous_day or sender_key != previous_sender),
            )
            views.append(view)
            previous_day = day
            previous_sender = sender_key
        return views

    def media_view(self, row) -> MediaView | None:
        if not row["media_type"]:
            return None
        view = MediaView(type=row["media_type"], label=media_label(row["media_type"]), note=row["media_note"])
        if row["media_path"]:
            source = self.config.data_dir / row["media_path"]
            if source.is_file():
                view.source = source
                view.size = source.stat().st_size
                view.name = source.name
                view.href = f"media/{source.name}"
            else:
                view.note = view.note or "файл не найден на диске"
        elif row["media_size"]:
            view.size = row["media_size"]
        return view

    def split_parts(self, views: list[MessageView]) -> list[list[MessageView]]:
        budget = self.config.export_part_bytes
        parts: list[list[MessageView]] = [[]]
        used = 0
        for view in views:
            media = view.media
            size = media.size if media and media.source else 0
            overhead = 200 + len(view.text or "") // 2
            if size > budget:
                media.note = f"файл {media.size_label} не поместился в архив, хранится на сервере"
                media.source = media.href = None
                size = 0
            if used + size + overhead > budget and parts[-1]:
                parts.append([])
                used = 0
            parts[-1].append(view)
            used += size + overhead
        return parts

    def write_parts(self, chat_id: int, chat, views: list[MessageView]) -> list[Path]:
        self.config.exports_dir.mkdir(parents=True, exist_ok=True)
        title = (chat["title"] if chat else None) or str(chat_id)
        stamp = datetime.now(timezone.utc).astimezone(self.config.timezone)
        base = f"{slugify(title, 'chat')}_{chat_id}_{stamp:%Y%m%d_%H%M%S}"
        parts = self.split_parts(views)
        total = len(parts)
        template = self.env.get_template("chat.html")
        stats = {
            "messages": len(views),
            "deleted": sum(1 for v in views if v.deleted_at),
            "edited": sum(1 for v in views if v.edited_at),
            "media": sum(1 for v in views if v.media and v.media.source),
        }

        paths: list[Path] = []
        for index, part in enumerate(parts, 1):
            if part:
                part[0].new_day = True
                part[0].show_sender = chat is None or chat["type"] != "private"
            html = template.render(
                title=title,
                username=chat["username"] if chat else None,
                chat_type=chat["type"] if chat else "unknown",
                chat_id=chat_id,
                messages=part,
                part=index,
                total=total,
                stats=stats,
                generated=fmt_dt(stamp, self.config.timezone),
                period=(part[0].full_time, part[-1].full_time) if part else None,
            )
            name = f"{base}_part{index}.zip" if total > 1 else f"{base}.zip"
            path = self.config.exports_dir / name
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("chat.html", html)
                for view in part:
                    media = view.media
                    if media and media.source and media.href:
                        compress = (
                            zipfile.ZIP_STORED
                            if media.source.suffix.lower() in STORED_EXTENSIONS
                            else zipfile.ZIP_DEFLATED
                        )
                        archive.write(media.source, media.href, compress_type=compress)
            paths.append(path)
        return paths
