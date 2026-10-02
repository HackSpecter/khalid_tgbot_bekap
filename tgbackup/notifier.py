from __future__ import annotations

import asyncio
import html
import io
import logging
import zipfile
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .crypto import Vault

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
NO_CAPTION_TYPES = {"sticker", "video_note"}


def esc(value: object) -> str:
    return html.escape(str(value), quote=False) if value is not None else ""


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


class Notifier:
    def __init__(
        self,
        client: TelegramClient,
        target: int | str,
        vault: Vault | None = None,
        *,
        supports_buttons: bool = False,
    ) -> None:
        self.client = client
        self.target = target
        self.vault = vault
        self.supports_buttons = supports_buttons
        self._lock = asyncio.Lock()

    def _buttons(self, buttons):
        return buttons if self.supports_buttons and buttons else None

    async def _call(self, factory) -> None:
        for attempt in range(3):
            try:
                await factory()
                return
            except FloodWaitError as exc:
                log.warning("Notifier flood wait %ss", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except Exception:
                log.exception("Notifier send failed (attempt %s)", attempt + 1)
                await asyncio.sleep(2)

    async def send_text(self, text: str, buttons=None) -> None:
        chunks = split_text(text)
        async with self._lock:
            for index, chunk in enumerate(chunks):
                markup = self._buttons(buttons) if index == len(chunks) - 1 else None
                await self._call(
                    lambda chunk=chunk, markup=markup: self.client.send_message(
                        self.target, chunk, parse_mode="html", link_preview=False, buttons=markup
                    )
                )

    async def send_file(
        self,
        file: Path | io.BytesIO,
        caption: str = "",
        *,
        as_document: bool = False,
        voice_note: bool = False,
        video_note: bool = False,
        buttons=None,
    ) -> None:
        caption_part, rest = caption, ""
        if len(caption) > CAPTION_LIMIT:
            caption_part, rest = "", caption
        source = str(file) if isinstance(file, Path) else file

        async def send():
            if isinstance(source, io.BytesIO):
                source.seek(0)
            await self.client.send_file(
                self.target,
                source,
                caption=caption_part,
                parse_mode="html",
                force_document=as_document,
                voice_note=voice_note,
                video_note=video_note,
                buttons=None if rest else self._buttons(buttons),
            )

        async with self._lock:
            await self._call(send)
        if rest:
            await self.send_text(rest, buttons=buttons)

    async def send_media(
        self, rel_path: str, caption: str = "", media_type: str | None = None, buttons=None
    ) -> None:
        if self.vault is None or not self.vault.exists(rel_path):
            await self.send_text(caption, buttons=buttons)
            return
        data = await asyncio.to_thread(self.vault.read, rel_path)
        buffer = io.BytesIO(data)
        buffer.name = self.vault.logical_name(rel_path)
        if media_type in NO_CAPTION_TYPES and caption:
            await self.send_text(caption, buttons=buttons)
            caption = ""
            buttons = None
        await self.send_file(
            buffer,
            caption,
            as_document=media_type == "document",
            voice_note=media_type == "voice",
            video_note=media_type == "video_note",
            buttons=buttons,
        )

    async def send_export(self, parts: list[Path], title: str, intro: str = "", buttons=None) -> None:
        total = len(parts)
        for index, part in enumerate(parts, 1):
            suffix = f" — часть {index}/{total}" if total > 1 else ""
            pdf = await asyncio.to_thread(read_pdf, part)
            if pdf is not None:
                buffer = io.BytesIO(pdf)
                buffer.name = part.with_suffix(".pdf").name
                caption = f"{intro}\n\n" if intro and index == 1 else ""
                await self.send_file(buffer, f"{caption}📄 <b>{esc(title)}</b>{suffix} — читать", as_document=True)
            else:
                if intro and index == 1:
                    await self.send_text(intro)
            last = index == total
            await self.send_file(
                part,
                f"🔐 Архив с доказательствами{suffix}: HTML, медиа, подписи, метки времени. "
                "Хранить целиком, проверка — verify_export.py внутри.",
                as_document=True,
                buttons=buttons if last else None,
            )


def read_pdf(archive_path: Path) -> bytes | None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return archive.read("chat.pdf")
    except KeyError:
        return None
