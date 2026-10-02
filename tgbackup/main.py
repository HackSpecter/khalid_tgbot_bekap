from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from telethon import TelegramClient

from .commands import Commands
from .config import Config, ConfigError, load_config, setup_logging
from .db import Database
from .deletions import DeletionAggregator
from .exporter import Exporter
from .handlers import Tracker
from .media import MediaDownloader
from .notifier import Notifier

log = logging.getLogger("tgbackup")

RECONNECT_DELAY = 15


def create_user_client(config: Config, *, catch_up: bool = False) -> TelegramClient:
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        catch_up=catch_up,
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
        flood_sleep_threshold=120,
        request_retries=5,
    )


def secure_sessions(config: Config) -> None:
    for base in (config.session_path, config.notifier_session_path):
        for path in (Path(f"{base}.session"), Path(f"{base}.session-journal")):
            if path.exists():
                os.chmod(path, 0o600)


async def keep_connected(client: TelegramClient, name: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        disconnected = asyncio.ensure_future(client.disconnected)
        stopping = asyncio.ensure_future(stop.wait())
        await asyncio.wait({disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if stop.is_set():
            disconnected.cancel()
            return
        with contextlib.suppress(Exception):
            disconnected.result()
        log.warning("%s disconnected, reconnecting in %ss", name, RECONNECT_DELAY)
        while not stop.is_set():
            await asyncio.sleep(RECONNECT_DELAY)
            try:
                await client.connect()
                if client.is_connected():
                    log.info("%s reconnected", name)
                    break
            except Exception as exc:
                log.warning("%s reconnect failed: %s", name, exc)


async def run(config: Config) -> int:
    os.umask(0o077)
    config.ensure_dirs()

    db = Database(config.db_path)
    await db.connect()
    client = create_user_client(config, catch_up=True)

    bot: TelegramClient | None = None
    if config.notify_bot_token:
        bot = TelegramClient(
            str(config.notifier_session_path),
            config.api_id,
            config.api_hash,
            connection_retries=10,
            retry_delay=5,
            auto_reconnect=True,
            flood_sleep_threshold=300,
        )
        notifier = Notifier(bot, config.owner_id)
    else:
        notifier = Notifier(client, "me")

    exporter = Exporter(db, config)
    deletions = DeletionAggregator(config, db, exporter, notifier)
    downloader = MediaDownloader(client, config.media_dir, config.media_max_bytes)
    tracker = Tracker(config, db, downloader, notifier, deletions)
    tracker.register(client)
    commands = Commands(config, db, exporter, notifier, client, tracker)

    await client.connect()
    if not await client.is_user_authorized():
        log.error("Session is not authorized. Run `python login.py` interactively first.")
        await client.disconnect()
        await db.close()
        return 2
    me = await client.get_me()
    tracker.me_id = me.id
    log.info("Logged in as %s (%s)", me.username or me.first_name, me.id)

    if bot is not None:
        commands.register_bot(bot, config.owner_id)
        await bot.start(bot_token=config.notify_bot_token)
    else:
        commands.register_saved(client, me.id)
    secure_sessions(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await client.catch_up()
    log.info("Backup is running (notifications via %s)", "bot" if bot else "Saved Messages")
    await notifier.send_text("✅ Бэкап переписок запущен")

    watchers = [asyncio.create_task(keep_connected(client, "userbot", stop))]
    if bot is not None:
        watchers.append(asyncio.create_task(keep_connected(bot, "notifier", stop)))

    try:
        await stop.wait()
    finally:
        log.info("Shutting down")
        for task in watchers:
            task.cancel()
        with contextlib.suppress(Exception):
            await deletions.flush()
        for tg in (bot, client):
            if tg is not None:
                with contextlib.suppress(Exception):
                    await tg.disconnect()
        await db.close()
    return 0


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)
    setup_logging(config)
    try:
        code = asyncio.run(run(config))
    except KeyboardInterrupt:
        code = 0
    except Exception:
        log.exception("Fatal error")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
