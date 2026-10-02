from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient

from .anchoring import Anchorer
from .commands import Commands
from .config import Config, ConfigError, load_config, setup_logging
from .crypto import KeyRing, Signer, Vault
from .db import Database
from .deletions import DeletionAggregator
from .evidence import EvidenceLog
from .exporter import Exporter
from .handlers import Tracker
from .maintenance import run_maintenance
from .media import MediaDownloader
from .notifier import Notifier
from .profiles import ProfileTracker
from .settings import Settings

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


@dataclass
class Storage:
    db: Database
    keyring: KeyRing
    vault: Vault
    evidence: EvidenceLog
    signer: Signer

    async def close(self) -> None:
        await self.db.close()


async def open_storage(config: Config, *, maintenance: bool = True) -> Storage:
    os.umask(0o077)
    config.ensure_dirs()
    keyring = KeyRing.from_secret(config.encryption_key)
    if not keyring.enabled:
        log.warning("ENCRYPTION_KEY is not set: database and media are stored UNENCRYPTED")
    db = Database(config.db_path, keyring.db_key_hex)
    await db.connect()
    vault = Vault(config.data_dir, keyring)
    evidence = EvidenceLog(db)
    signer = Signer.load_or_create(config.keys_dir, keyring)
    if maintenance:
        await run_maintenance(db, evidence, vault)
    return Storage(db, keyring, vault, evidence, signer)


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
    if not config.encryption_key and not config.allow_unencrypted:
        log.error("ENCRYPTION_KEY is required (or set ALLOW_UNENCRYPTED=true explicitly)")
        return 1
    storage = await open_storage(config)
    db = storage.db
    settings = await Settings(config, db).load()
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
        notifier = Notifier(bot, config.owner_id, storage.vault, supports_buttons=True)
    else:
        notifier = Notifier(client, "me", storage.vault)

    exporter = Exporter(db, config, storage.vault, storage.evidence, storage.signer)
    deletions = DeletionAggregator(config, db, exporter, notifier, storage.evidence, settings)
    downloader = MediaDownloader(client, storage.vault, config.media_max_bytes)
    profiles = (
        ProfileTracker(config, db, storage.evidence, downloader, notifier, client, settings)
        if config.profile_tracking
        else None
    )
    tracker = Tracker(config, db, downloader, storage.evidence, notifier, deletions, profiles, settings)
    tracker.register(client)
    anchorer = Anchorer(config, db, storage.evidence)
    commands = Commands(config, db, exporter, notifier, client, tracker, anchorer, settings)

    await client.connect()
    if not await client.is_user_authorized():
        log.error("Session is not authorized. Run `python login.py` interactively first.")
        await client.disconnect()
        await storage.close()
        return 2
    me = await client.get_me()
    tracker.me_id = me.id
    if profiles is not None:
        profiles.me_id = me.id
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

    watchers = [
        asyncio.create_task(keep_connected(client, "userbot", stop)),
        asyncio.create_task(anchorer.run_scheduler(stop)),
    ]
    if profiles is not None:
        watchers.append(asyncio.create_task(profiles.run_scheduler(stop)))
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
        await storage.close()
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
