from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopePeer

from .anchoring import Anchorer
from .config import Config
from .db import Database
from .evidence import ChainVerifier
from .exporter import Exporter
from .formatting import chat_label, fmt_dt, human_size, media_label, sender_label
from . import keyboards
from .handlers import Tracker
from .notifier import Notifier, esc
from .settings import TOGGLES, Settings
from .sync import sync_history

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.S)

HELP = """<b>Бэкап переписок</b>

/export &lt;@username | chat_id | название&gt; — экспорт: PDF + архив с доказательствами
/deleted &lt;чат&gt; [N] — последние удалённые сообщения
/search &lt;текст&gt; — поиск по сохранённым сообщениям
/stats — статистика хранилища
/sync &lt;чат | all&gt; [лимит] — догрузить историю
/profile &lt;чат&gt; — история профиля собеседника
/verify — проверить хеш-цепочку
/anchor — заякорить цепочку прямо сейчас (RFC 3161 + OpenTimestamps)
/settings — настройки кнопками
/mute, /unmute &lt;чат&gt; — выключить/вернуть уведомления по чату
/exclude, /include &lt;чат&gt; — перестать/снова сохранять чат
/help — эта справка"""


@dataclass
class CallbackResult:
    toast: str = ""
    text: str | None = None
    buttons: list | None = None
    edit_buttons_only: bool = False


def dir_size(path: Path) -> tuple[int, int]:
    total = files = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                total += item.stat().st_size
    return total, files


COMMAND_MENU = [
    ("help", "справка по всем командам"),
    ("export", "экспорт чата: PDF + архив доказательств"),
    ("deleted", "удалённые сообщения чата"),
    ("search", "поиск по сохранённым сообщениям"),
    ("stats", "статистика хранилища"),
    ("profile", "история профиля собеседника"),
    ("settings", "настройки кнопками"),
    ("mute", "не уведомлять о чате"),
    ("unmute", "вернуть уведомления по чату"),
    ("exclude", "перестать сохранять чат"),
    ("include", "снова сохранять чат"),
    ("sync", "догрузить историю чата"),
    ("verify", "проверить хеш-цепочку"),
    ("anchor", "поставить метку времени сейчас"),
]


class Commands:
    def __init__(
        self,
        config: Config,
        db: Database,
        exporter: Exporter,
        notifier: Notifier,
        user_client: TelegramClient,
        tracker: Tracker,
        anchorer: Anchorer | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or tracker.settings
        self.db = db
        self.exporter = exporter
        self.notifier = notifier
        self.user_client = user_client
        self.tracker = tracker
        self.anchorer = anchorer
        self._sync_task: asyncio.Task | None = None
        self.handlers = {
            "start": self.cmd_help,
            "help": self.cmd_help,
            "export": self.cmd_export,
            "deleted": self.cmd_deleted,
            "stats": self.cmd_stats,
            "search": self.cmd_search,
            "sync": self.cmd_sync,
            "profile": self.cmd_profile,
            "verify": self.cmd_verify,
            "anchor": self.cmd_anchor,
            "settings": self.cmd_settings,
            "mute": lambda arg: self.cmd_mute(arg, True),
            "unmute": lambda arg: self.cmd_mute(arg, False),
            "exclude": lambda arg: self.cmd_exclude(arg, True),
            "include": lambda arg: self.cmd_exclude(arg, False),
        }
        self._background: set[asyncio.Task] = set()

    def register_bot(self, bot: TelegramClient, owner_id: int) -> None:
        bot.add_event_handler(self.dispatch, events.NewMessage(incoming=True, from_users=owner_id, pattern=COMMAND_RE))
        bot.add_event_handler(self.on_callback, events.CallbackQuery(func=lambda e: e.sender_id == owner_id))

    async def publish_menu(self, bot: TelegramClient, owner_id: int) -> None:
        commands = [BotCommand(name, description) for name, description in COMMAND_MENU]
        try:
            peer = await bot.get_input_entity(owner_id)
            await bot(SetBotCommandsRequest(scope=BotCommandScopePeer(peer), lang_code="", commands=commands))
            await bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=commands))
        except Exception:
            log.exception("Cannot publish bot command menu")

    def spawn(self, coro) -> None:
        task = asyncio.create_task(self._guarded(coro))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _guarded(self, coro) -> None:
        try:
            await coro
        except Exception as exc:
            log.exception("Background command failed")
            await self.notifier.send_text(f"❌ Ошибка: {esc(exc)}")

    async def on_callback(self, event: events.CallbackQuery.Event) -> None:
        action, value = keyboards.parse(event.data)
        try:
            result = await self.handle_callback(action, value)
        except Exception as exc:
            log.exception("Callback %s failed", action)
            await event.answer(f"Ошибка: {exc}"[:180], alert=True)
            return
        try:
            if result.edit_buttons_only:
                await event.edit(buttons=result.buttons)
            elif result.text is not None:
                await event.edit(result.text, buttons=result.buttons, parse_mode="html")
        except Exception:
            log.debug("Cannot edit callback message", exc_info=True)
        await event.answer(result.toast)

    async def handle_callback(self, action: str, value: str) -> CallbackResult:
        if action == keyboards.EXPORT:
            self.spawn(self.export_chat(int(value)))
            return CallbackResult("⏳ Готовлю экспорт…")
        if action == keyboards.DELETED:
            self.spawn(self.show_deleted(int(value)))
            return CallbackResult("📜 Собираю удалённые…")
        if action == keyboards.MUTE:
            await self.settings.set_muted(int(value), True)
            return CallbackResult("🔕 Уведомления по чату выключены", buttons=keyboards.muted_notice(int(value)),
                                  edit_buttons_only=True)
        if action == keyboards.UNMUTE:
            await self.settings.set_muted(int(value), False)
            return CallbackResult("🔔 Уведомления включены", buttons=keyboards.chat_actions(int(value)),
                                  edit_buttons_only=True)
        if action == keyboards.TOGGLE:
            enabled = await self.settings.toggle(value)
            text, buttons = self.settings_view()
            return CallbackResult(f"{TOGGLES[value]}: {'вкл' if enabled else 'выкл'}", text, buttons)
        if action == keyboards.MENU:
            if value == "muted":
                return CallbackResult("", *await self.chat_list_view("muted"))
            if value == "excluded":
                return CallbackResult("", *await self.chat_list_view("excluded"))
            return CallbackResult("", *self.settings_view())
        if action == keyboards.LIST_UNMUTE:
            await self.settings.set_muted(int(value), False)
            return CallbackResult("🔔 Уведомления включены", *await self.chat_list_view("muted"))
        if action == keyboards.INCLUDE:
            await self.settings.set_excluded(int(value), False)
            return CallbackResult("✅ Чат снова сохраняется", *await self.chat_list_view("excluded"))
        return CallbackResult("Неизвестная кнопка")

    def settings_view(self) -> tuple[str, list]:
        lines = ["⚙️ <b>Настройки</b>", "", "Что сохранять и о чём уведомлять. Изменения применяются сразу."]
        return "\n".join(lines), keyboards.settings_main(self.settings)

    async def chat_list_view(self, kind: str) -> tuple[str, list]:
        ids = sorted(self.settings.muted if kind == "muted" else self.settings.excluded)
        entries = []
        for chat_id in ids:
            chat = await self.db.get_chat(chat_id)
            entries.append((chat_id, chat_label(chat, chat_id)))
        if kind == "muted":
            title = "🔕 <b>Заглушённые чаты</b>\nНажмите, чтобы вернуть уведомления."
            buttons = keyboards.chat_list(entries, keyboards.LIST_UNMUTE, "🔔")
        else:
            title = "🚫 <b>Исключённые чаты</b> (не сохраняются)\nНажмите, чтобы снова сохранять."
            buttons = keyboards.chat_list(entries, keyboards.INCLUDE, "✅")
        if not entries:
            title += "\n\nСписок пуст."
        return title, buttons

    def register_saved(self, client: TelegramClient, me_id: int) -> None:
        client.add_event_handler(
            self.dispatch,
            events.NewMessage(outgoing=True, pattern=COMMAND_RE, func=lambda e: e.chat_id == me_id),
        )

    async def dispatch(self, event: events.NewMessage.Event) -> None:
        name = event.pattern_match.group(1).lower()
        arg = (event.pattern_match.group(2) or "").strip()
        handler = self.handlers.get(name)
        if handler is None:
            return
        try:
            await handler(arg)
        except Exception as exc:
            log.exception("Command /%s failed", name)
            await self.notifier.send_text(f"❌ Ошибка: {esc(exc)}")

    async def resolve_chat(self, query: str):
        chat = await self.db.find_chat(query)
        if chat is not None:
            return chat
        try:
            target = int(query) if query.lstrip("-").isdigit() else query
            entity = await self.user_client.get_entity(target)
        except Exception:
            return None
        return await self.db.get_chat(utils.get_peer_id(entity))

    async def cmd_help(self, arg: str) -> None:
        await self.notifier.send_text(HELP)

    async def cmd_export(self, arg: str) -> None:
        if not arg:
            await self.notifier.send_text("Использование: /export &lt;@username | chat_id | название&gt;")
            return
        chat = await self.resolve_chat(arg)
        if chat is None:
            await self.notifier.send_text(f"Чат «{esc(arg)}» не найден в базе")
            return
        await self.export_chat(chat["id"])

    async def export_chat(self, chat_id: int) -> None:
        chat = await self.db.get_chat(chat_id)
        label = chat_label(chat, chat_id)
        await self.notifier.send_text(f"⏳ Готовлю экспорт: {esc(label)}")
        parts = await self.exporter.export(chat_id)
        await self.notifier.send_export(parts, label)

    async def cmd_deleted(self, arg: str) -> None:
        if not arg:
            await self.notifier.send_text("Использование: /deleted &lt;чат&gt; [N]")
            return
        limit = 30
        query = arg
        head, _, tail = arg.rpartition(" ")
        if head and tail.isdigit():
            query, limit = head, min(int(tail), 500)
        chat = await self.resolve_chat(query)
        if chat is None:
            await self.notifier.send_text(f"Чат «{esc(query)}» не найден в базе")
            return
        await self.show_deleted(chat["id"], limit)

    async def show_deleted(self, chat_id: int, limit: int = 30) -> None:
        chat = await self.db.get_chat(chat_id)
        rows = await self.db.deleted_messages(chat_id, limit)
        label = esc(chat_label(chat, chat_id))
        if not rows:
            await self.notifier.send_text(f"В чате {label} удалённых сообщений нет")
            return
        tz = self.config.timezone
        lines = [f"🗑 <b>Удалённые в {label}</b> ({len(rows)})", ""]
        for row in rows:
            media = f"[{esc(media_label(row['media_type']))}] " if row["media_type"] else ""
            lines.append(
                f"<b>{esc(sender_label(row))}</b> · {fmt_dt(row['date'], tz, '%d.%m.%y %H:%M')}"
                f" → удалено {fmt_dt(row['deleted_at'], tz, '%d.%m.%y %H:%M')}"
            )
            lines.append(f"{media}{esc(row['text'] or row['service_action'] or '')}".strip() or "—")
            lines.append("")
        await self.notifier.send_text("\n".join(lines), buttons=[keyboards.chat_actions(chat_id)[0][:1]])

    async def cmd_stats(self, arg: str) -> None:
        stats = await self.db.stats()
        media_bytes, media_files = await asyncio.to_thread(dir_size, self.config.media_dir)
        lines = [
            "📊 <b>Статистика</b>",
            f"Сообщений: {stats.messages}",
            f"Чатов: {stats.chats}",
            f"Пользователей: {stats.users}",
            f"Удалённых: {stats.deleted}",
            f"Правок: {stats.edits}",
            f"Медиа: {media_files} файлов, {human_size(media_bytes)}",
            f"База: {human_size(stats.db_bytes)} ({'SQLCipher' if stats.encrypted else 'без шифрования!'})",
            f"Записей в цепочке: {stats.evidence}",
            f"Снимков профилей: {stats.profiles}",
            f"Якорей: {stats.anchors}",
        ]
        last = await self.db.last_anchor()
        if last is not None:
            tz = self.config.timezone
            lines.append(
                f"Последний якорь: seq {last['seq']}, {fmt_dt(last['created_at'], tz)}, "
                f"TSA {'✅' if last['tsr_file'] else '❌'}, OTS {esc(last['ots_status'] or '—')}"
            )
        await self.notifier.send_text("\n".join(lines))

    async def cmd_search(self, arg: str) -> None:
        if len(arg) < 2:
            await self.notifier.send_text("Использование: /search &lt;текст&gt; (минимум 2 символа)")
            return
        rows = await self.db.search(arg, limit=25)
        if not rows:
            await self.notifier.send_text(f"Ничего не найдено по «{esc(arg)}»")
            return
        tz = self.config.timezone
        lines = [f"🔎 <b>Найдено</b> ({len(rows)}):", ""]
        for row in rows:
            text = (row["text"] or "").replace("\n", " ")
            if len(text) > 300:
                text = text[:299] + "…"
            flags = " 🗑" if row["deleted"] else ""
            flags += " ✏️" if row["edited"] else ""
            lines.append(
                f"<b>{esc(row['chat_title'] or row['chat_id'])}</b> · {esc(sender_label(row))}"
                f" · {fmt_dt(row['date'], tz, '%d.%m.%y %H:%M')}{flags}"
            )
            lines.append(esc(text))
            lines.append("")
        await self.notifier.send_text("\n".join(lines))

    async def cmd_sync(self, arg: str) -> None:
        if self._sync_task and not self._sync_task.done():
            await self.notifier.send_text("Синхронизация уже идёт")
            return
        parts = arg.split()
        limit = None
        if len(parts) > 1 and parts[-1].isdigit():
            limit = int(parts.pop())
        target = " ".join(parts)
        if not target:
            await self.notifier.send_text("Использование: /sync &lt;@username | chat_id | all&gt; [лимит]")
            return
        targets = None if target.lower() == "all" else [target]
        self._sync_task = asyncio.create_task(self._run_sync(targets, limit))

    async def _run_sync(self, targets: list[str] | None, limit: int | None) -> None:
        async def progress(text: str) -> None:
            log.info("Sync: %s", text)

        await self.notifier.send_text("⏳ Синхронизация запущена")
        try:
            result = await sync_history(self.user_client, self.tracker, targets, limit=limit, progress=progress)
        except Exception as exc:
            log.exception("Sync failed")
            await self.notifier.send_text(f"❌ Синхронизация упала: {esc(exc)}")
            return
        await self.notifier.send_text(
            f"✅ Синхронизация завершена: чатов {result.chats}, сообщений {result.messages}, ошибок {result.failed}"
        )

    async def cmd_profile(self, arg: str) -> None:
        if not arg:
            await self.notifier.send_text("Использование: /profile &lt;@username | chat_id&gt;")
            return
        chat = await self.resolve_chat(arg)
        if chat is None or chat["type"] != "private":
            await self.notifier.send_text(f"Личный чат «{esc(arg)}» не найден")
            return
        history = await self.db.profile_history(chat["id"])
        if not history:
            await self.notifier.send_text("История профиля пока пуста")
            return
        tz = self.config.timezone
        lines = [f"👤 <b>История профиля</b> {esc(chat_label(chat, chat['id']))} (id {chat['id']})", ""]
        for item in history:
            changes = json.loads(item["changes"])
            described = []
            for field_name, (old, new) in changes.items():
                if field_name == "photo_id":
                    described.append("фото: " + ("новое" if new else "удалено/скрыто"))
                else:
                    described.append(f"{field_name}: {esc(old or '—')} → {esc(new or '—')}")
            lines.append(f"<b>{fmt_dt(item['observed_at'], tz)}</b> ({esc(item['source'])})")
            lines.append("; ".join(described) or "—")
            lines.append("")
        await self.notifier.send_text("\n".join(lines))

    async def cmd_verify(self, arg: str) -> None:
        await self.notifier.send_text("⏳ Проверяю цепочку…")
        verifier = ChainVerifier()
        async for entry in self.tracker.evidence.iter_all():
            verifier.feed(entry)
        if verifier.issues:
            shown = "\n".join(f"seq {i.seq}: {esc(i.problem)}" for i in verifier.issues[:20])
            await self.notifier.send_text(
                f"❌ Нарушения целостности: {len(verifier.issues)}\n{shown}\n\nПолный отчёт: python verify.py"
            )
            return
        head = verifier.last
        await self.notifier.send_text(
            f"✅ Цепочка цела: {verifier.count} записей\n"
            f"Голова: seq {head.seq if head else 0}\n<code>{head.entry_hash if head else '—'}</code>\n\n"
            "Полная проверка медиа и меток времени: python verify.py"
        )

    async def cmd_anchor(self, arg: str) -> None:
        if self.anchorer is None:
            await self.notifier.send_text("Заякоривание недоступно")
            return
        await self.notifier.send_text("⏳ Заякориваю голову цепочки…")
        result = await self.anchorer.anchor_now(force=arg.strip().lower() == "force")
        if result is None:
            await self.notifier.send_text("Цепочка не менялась с последнего якоря (для повтора: /anchor force)")
            return
        await self.notifier.send_text(
            f"⚓️ Якорь seq {result['seq']}\n<code>{result['entry_hash']}</code>\n"
            f"RFC 3161: {esc(result.get('tsa_time') or result.get('tsa_error') or 'выключено')}\n"
            f"OpenTimestamps: {esc(result.get('ots_status') or 'выключено')}"
        )

    async def cmd_settings(self, arg: str) -> None:
        text, buttons = self.settings_view()
        await self.notifier.send_text(text, buttons=buttons)

    async def cmd_mute(self, arg: str, muted: bool) -> None:
        chat = await self.resolve_chat(arg) if arg else None
        if chat is None:
            await self.notifier.send_text(f"Использование: /{'mute' if muted else 'unmute'} &lt;чат&gt;")
            return
        await self.settings.set_muted(chat["id"], muted)
        state = "выключены 🔕" if muted else "включены 🔔"
        await self.notifier.send_text(f"Уведомления по {esc(chat_label(chat, chat['id']))} {state}")

    async def cmd_exclude(self, arg: str, excluded: bool) -> None:
        chat = await self.resolve_chat(arg) if arg else None
        if chat is None:
            await self.notifier.send_text(f"Использование: /{'exclude' if excluded else 'include'} &lt;чат&gt;")
            return
        await self.settings.set_excluded(chat["id"], excluded)
        state = "больше не сохраняется 🚫" if excluded else "снова сохраняется ✅"
        await self.notifier.send_text(f"{esc(chat_label(chat, chat['id']))} {state}")
