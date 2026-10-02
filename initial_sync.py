import argparse
import asyncio
import logging
import sys

from tgbackup.config import ConfigError, load_config, setup_logging
from tgbackup.db import Database
from tgbackup.handlers import Tracker
from tgbackup.main import create_user_client
from tgbackup.media import MediaDownloader
from tgbackup.sync import sync_history

log = logging.getLogger("initial_sync")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Первичная загрузка истории чатов в БД")
    parser.add_argument("chats", nargs="*", help="@username или chat_id; без аргументов — все диалоги по фильтрам TRACK_*")
    parser.add_argument("--limit", type=int, default=None, help="максимум сообщений на чат")
    parser.add_argument("--no-media", action="store_true", help="не скачивать медиа")
    parser.add_argument("--pause", type=float, default=1.0, help="пауза между пачками запросов, сек")
    parser.add_argument("--chat-pause", type=float, default=3.0, help="пауза между чатами, сек")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    setup_logging(config)
    client = create_user_client(config)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Сессия не авторизована: сначала выполните python login.py")

    async with Database(config.db_path) as db:
        downloader = MediaDownloader(client, config.media_dir, config.media_max_bytes)
        tracker = Tracker(config, db, downloader)
        tracker.me_id = (await client.get_me()).id

        async def progress(text: str) -> None:
            log.info(text)

        result = await sync_history(
            client,
            tracker,
            args.chats or None,
            limit=args.limit,
            with_media=not args.no_media,
            pause=args.pause,
            chat_pause=args.chat_pause,
            progress=progress,
        )
    await client.disconnect()
    log.info("Готово: чатов %s, сообщений %s, ошибок %s", result.chats, result.messages, result.failed)


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")
    except KeyboardInterrupt:
        pass
