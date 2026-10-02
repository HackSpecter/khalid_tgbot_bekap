from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError

from .handlers import Tracker, entity_title, entity_type

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


@dataclass
class SyncResult:
    chats: int = 0
    messages: int = 0
    failed: int = 0


def parse_target(value: str) -> int | str:
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


async def resolve_targets(client: TelegramClient, tracker: Tracker, targets: Sequence[str] | None) -> list:
    if targets:
        entities = []
        for target in targets:
            try:
                entities.append(await client.get_entity(parse_target(target)))
            except Exception as exc:
                log.error("Cannot resolve %s: %s", target, exc)
        return entities

    entities = []
    async for dialog in client.iter_dialogs():
        chat_id = utils.get_peer_id(dialog.entity)
        if tracker.is_ignored_chat(chat_id) or not tracker.kind_enabled(entity_type(dialog.entity)):
            continue
        entities.append(dialog.entity)
    return entities


async def sync_chat(
    client: TelegramClient,
    tracker: Tracker,
    entity,
    *,
    limit: int | None = None,
    with_media: bool = True,
    pause: float = 1.0,
) -> int:
    stored = 0
    offset_id = 0
    remaining = limit
    while True:
        try:
            async for message in client.iter_messages(
                entity, limit=remaining, offset_id=offset_id, wait_time=pause
            ):
                offset_id = message.id
                try:
                    if await tracker.store(message, chat=entity, with_media=with_media):
                        stored += 1
                except Exception:
                    log.exception("Sync: failed to store %s/%s", utils.get_peer_id(entity), message.id)
                if remaining is not None:
                    remaining -= 1
            return stored
        except FloodWaitError as exc:
            log.warning("Sync flood wait %ss on %s", exc.seconds, utils.get_peer_id(entity))
            await asyncio.sleep(exc.seconds + 1)
            if remaining is not None and remaining <= 0:
                return stored


async def sync_history(
    client: TelegramClient,
    tracker: Tracker,
    targets: Sequence[str] | None = None,
    *,
    limit: int | None = None,
    with_media: bool = True,
    pause: float = 1.0,
    chat_pause: float = 3.0,
    progress: Progress | None = None,
) -> SyncResult:
    result = SyncResult()
    entities = await resolve_targets(client, tracker, targets)
    if progress:
        await progress(f"Чатов к загрузке: {len(entities)}")

    for index, entity in enumerate(entities, 1):
        title = entity_title(entity) or str(utils.get_peer_id(entity))
        try:
            count = await sync_chat(client, tracker, entity, limit=limit, with_media=with_media, pause=pause)
            result.chats += 1
            result.messages += count
            log.info("Synced %s (%s): %s messages", title, utils.get_peer_id(entity), count)
            if progress:
                await progress(f"[{index}/{len(entities)}] {title}: {count}")
        except Exception:
            result.failed += 1
            log.exception("Sync failed for %s", title)
        await asyncio.sleep(chat_pause)
    return result
