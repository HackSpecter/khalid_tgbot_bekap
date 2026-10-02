import asyncio
import sys

from tgbackup.config import ConfigError, load_config
from tgbackup.main import create_user_client, secure_sessions


async def login() -> None:
    config = load_config()
    config.ensure_dirs()
    client = create_user_client(config)
    await client.start()
    me = await client.get_me()
    secure_sessions(config)
    print(f"OK: вошли как {me.first_name} (@{me.username}), id={me.id}")
    print(f"Сессия: {config.session_path}.session (права 600)")
    print(f"Впишите OWNER_ID={me.id} в .env, если ещё не сделали")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(login())
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")
