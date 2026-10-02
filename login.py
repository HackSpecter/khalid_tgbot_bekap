import argparse
import asyncio
import json
import os
import sys

from telethon.errors import SessionPasswordNeededError

from tgbackup.config import ConfigError, load_config
from tgbackup.main import create_user_client, secure_sessions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Вход в аккаунт Telegram")
    parser.add_argument("--phone", help="номер в формате +992…; без флагов — интерактивный вход")
    parser.add_argument("--send-code", action="store_true", help="шаг 1: запросить код")
    parser.add_argument("--code", help="шаг 2: код из Telegram (пароль 2FA — в переменной TG_PASSWORD)")
    return parser.parse_args()


async def report(client, config) -> None:
    me = await client.get_me()
    secure_sessions(config)
    print(f"OK: вошли как {me.first_name} (@{me.username}), id={me.id}")
    print(f"Сессия: {config.session_path}.session (права 600)")
    print(f"Впишите OWNER_ID={me.id} в .env, если ещё не сделали")


async def login(args: argparse.Namespace) -> int:
    config = load_config()
    config.ensure_dirs()
    os.umask(0o077)
    client = create_user_client(config)
    state_file = config.data_dir / "login_state.json"
    try:
        if not args.phone:
            await client.start()
            await report(client, config)
            return 0

        await client.connect()
        if await client.is_user_authorized():
            await report(client, config)
            return 0

        if args.send_code:
            sent = await client.send_code_request(args.phone)
            state_file.write_text(json.dumps({"phone": args.phone, "hash": sent.phone_code_hash}))
            print(f"Код отправлен ({type(sent.type).__name__}). Запустите: login.py --phone {args.phone} --code <код>")
            return 0

        password = os.getenv("TG_PASSWORD")
        if not args.code:
            if not password:
                print("Укажите --send-code, --code или пароль 2FA в TG_PASSWORD")
                return 1
            await client.sign_in(password=password)
            state_file.unlink(missing_ok=True)
            await report(client, config)
            return 0
        state = json.loads(state_file.read_text())
        try:
            await client.sign_in(phone=state["phone"], code=args.code, phone_code_hash=state["hash"])
        except SessionPasswordNeededError:
            if not password:
                print("Нужен пароль 2FA: запустите login.py --phone <номер> с паролем в TG_PASSWORD")
                return 3
            await client.sign_in(password=password)
        state_file.unlink(missing_ok=True)
        await report(client, config)
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(login(parse_args())))
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")
