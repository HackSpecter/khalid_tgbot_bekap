from __future__ import annotations

import asyncio
import html
import io
import logging
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
    def __init__(self, client: TelegramClient, target: int | str, vault: Vault | None = None) -> None:
        self.client = client
        self.target = target
        self.vault = vault
        self._lock = asyncio.Lock()

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

    async def send_text(self, text: str) -> None:
        async with self._lock:
            for chunk in split_text(text):
                await self._call(
                    lambda chunk=chunk: self.client.send_message(
                        self.target, chunk, parse_mode="html", link_preview=False
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
            )

        async with self._lock:
            await self._call(send)
        if rest:
            await self.send_text(rest)

    async def send_media(self, rel_path: str, caption: str = "", media_type: str | None = None) -> None:
        if self.vault is None or not self.vault.exists(rel_path):
            await self.send_text(caption)
            return
        data = await asyncio.to_thread(self.vault.read, rel_path)
        buffer = io.BytesIO(data)
        buffer.name = self.vault.logical_name(rel_path)
        if media_type in NO_CAPTION_TYPES and caption:
            await self.send_text(caption)
            caption = ""
        await self.send_file(
            buffer,
            caption,
            as_document=media_type == "document",
            voice_note=media_type == "voice",
            video_note=media_type == "video_note",
        )
