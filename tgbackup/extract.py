from __future__ import annotations

import json

from telethon import utils
from telethon.tl import types

from .db import to_iso
from .evidence import canonical_json

CALL_REASONS = {
    types.PhoneCallDiscardReasonMissed: "пропущен",
    types.PhoneCallDiscardReasonBusy: "занято",
    types.PhoneCallDiscardReasonHangup: "завершён",
    types.PhoneCallDiscardReasonDisconnect: "разрыв связи",
}


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def describe_action(action: object, outgoing: bool = False, reply_to: int | None = None) -> str:
    if isinstance(action, types.MessageActionPhoneCall):
        kind = "📹 Видеозвонок" if action.video else "📞 Звонок"
        direction = "исходящий" if outgoing else "входящий"
        parts = [f"{kind} ({direction})"]
        reason = CALL_REASONS.get(type(action.reason)) if action.reason else None
        if reason:
            parts.append(reason)
        if action.duration:
            parts.append(f"длительность {_duration(action.duration)}")
        return ", ".join(parts)
    if isinstance(action, types.MessageActionPinMessage):
        return f"📌 Закреплено сообщение #{reply_to}" if reply_to else "📌 Закреплено сообщение"
    if isinstance(action, types.MessageActionChatEditTitle):
        return f"Название изменено на «{action.title}»"
    if isinstance(action, types.MessageActionChatEditPhoto):
        return "Изменено фото чата"
    if isinstance(action, types.MessageActionChatDeletePhoto):
        return "Удалено фото чата"
    if isinstance(action, types.MessageActionChatAddUser):
        return "Добавлены участники: " + ", ".join(str(uid) for uid in action.users)
    if isinstance(action, types.MessageActionChatDeleteUser):
        return f"Удалён участник {action.user_id}"
    if isinstance(action, types.MessageActionChatJoinedByLink):
        return "Вступил по ссылке-приглашению"
    if isinstance(action, (types.MessageActionChatCreate, types.MessageActionChannelCreate)):
        return f"Создан чат «{action.title}»"
    if isinstance(action, types.MessageActionHistoryClear):
        return "История очищена"
    if isinstance(action, types.MessageActionScreenshotTaken):
        return "📸 Сделан скриншот"
    if isinstance(action, types.MessageActionSetMessagesTTL):
        return f"Автоудаление сообщений: {action.period} с" if action.period else "Автоудаление выключено"
    if isinstance(action, types.MessageActionContactSignUp):
        return "Присоединился к Telegram"
    if isinstance(action, types.MessageActionGroupCall):
        duration = _duration(action.duration)
        return f"Групповой звонок{', ' + duration if duration else ''}"
    name = type(action).__name__.removeprefix("MessageAction")
    return f"Сервисное событие: {name}"


def _reaction_key(reaction: object) -> str:
    if isinstance(reaction, types.ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, types.ReactionCustomEmoji):
        return f"custom:{reaction.document_id}"
    if isinstance(reaction, types.ReactionPaid):
        return "⭐"
    return type(reaction).__name__


def reactions_summary(reactions: types.MessageReactions | None) -> str | None:
    if reactions is None:
        return None
    counts = [{"r": _reaction_key(item.reaction), "n": item.count} for item in reactions.results or []]
    recent = [
        {"r": _reaction_key(item.reaction), "peer": utils.get_peer_id(item.peer_id), "date": item.date}
        for item in reactions.recent_reactions or []
    ]
    if not counts and not recent:
        return None
    return canonical_json({"counts": counts, "recent": recent})


def reactions_label(value: str | None) -> str:
    if not value:
        return ""
    data = json.loads(value)
    return " ".join(f"{item['r']}{item['n'] if item['n'] > 1 else ''}" for item in data.get("counts", []))


def forward_info(message) -> tuple[int | None, str | None, str | None]:
    fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return None, None, None
    from_id = utils.get_peer_id(fwd.from_id) if fwd.from_id else None
    name = fwd.from_name or fwd.post_author
    forward = getattr(message, "forward", None)
    if not name and forward is not None:
        entity = forward.sender or forward.chat
        if entity is not None:
            name = getattr(entity, "title", None) or " ".join(
                part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part
            )
    return from_id, name or None, to_iso(fwd.date)
