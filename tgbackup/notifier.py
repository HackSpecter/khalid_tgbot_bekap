from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


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
    def __init__(self, client: TelegramClient, target: int | str) -> None:
        self.client = client
        self.target = target
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

    async def send_file(self, path: Path, caption: str = "", *, as_document: bool = False) -> None:
        caption_part, rest = caption, ""
        if len(caption) > CAPTION_LIMIT:
            caption_part, rest = "", caption
        async with self._lock:
            await self._call(
                lambda: self.client.send_file(
                    self.target,
                    str(path),
                    caption=caption_part,
                    parse_mode="html",
                    force_document=as_document,
                )
            )
        if rest:
            await self.send_text(rest)
