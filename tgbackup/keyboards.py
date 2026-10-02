from __future__ import annotations

from telethon import Button

from .settings import TOGGLES, Settings

EXPORT = "x"
DELETED = "d"
MUTE = "m"
UNMUTE = "u"
TOGGLE = "t"
INCLUDE = "i"
LIST_UNMUTE = "v"
MENU = "s"


def data(action: str, value: object = "") -> bytes:
    return f"{action}:{value}".encode()


def parse(raw: bytes) -> tuple[str, str]:
    action, _, value = raw.decode().partition(":")
    return action, value


def chat_actions(chat_id: int) -> list[list[Button]]:
    return [
        [
            Button.inline("📦 Экспорт чата", data(EXPORT, chat_id)),
            Button.inline("📜 Все удалённые", data(DELETED, chat_id)),
        ],
        [Button.inline("🔕 Не уведомлять об этом чате", data(MUTE, chat_id))],
    ]


def muted_notice(chat_id: int) -> list[list[Button]]:
    return [[Button.inline("🔔 Вернуть уведомления", data(UNMUTE, chat_id))]]


def settings_main(settings: Settings) -> list[list[Button]]:
    rows = [
        [Button.inline(f"{'✅' if settings.get(key) else '⬜️'} {label}", data(TOGGLE, key))]
        for key, label in TOGGLES.items()
    ]
    rows.append(
        [
            Button.inline(f"🔕 Заглушённые ({len(settings.muted)})", data(MENU, "muted")),
            Button.inline(f"🚫 Исключённые ({len(settings.excluded)})", data(MENU, "excluded")),
        ]
    )
    return rows


def chat_list(entries: list[tuple[int, str]], action: str, icon: str) -> list[list[Button]]:
    rows = [[Button.inline(f"{icon} {title}"[:60], data(action, chat_id))] for chat_id, title in entries]
    rows.append([Button.inline("⬅️ Назад", data(MENU, "main"))])
    return rows
