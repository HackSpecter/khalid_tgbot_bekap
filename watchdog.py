import json
import logging
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("watchdog")

CHECK_INTERVAL = 60
STARTUP_GRACE = 120
STALE_AFTER = 180
OFFLINE_AFTER = 300
REMIND_EVERY = 6 * 3600
ANCHOR_MAX_AGE = 36 * 3600
DISK_LIMIT = 0.90
RESTART_LIMIT = 3


@dataclass
class Problem:
    key: str
    text: str


@dataclass
class State:
    active: dict = field(default_factory=dict)
    starts: deque = field(default_factory=lambda: deque(maxlen=20))
    last_started_at: str | None = None


def age(value: str | None, now: float) -> float | None:
    if not value:
        return None
    return now - datetime.fromisoformat(value).timestamp()


def human(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours < 48 else f"{hours // 24} дн"


def evaluate(heartbeat: dict | None, data_dir: Path, state: State, now: float) -> list[Problem]:
    problems: list[Problem] = []
    if heartbeat is None:
        return [Problem("stale", "🚨 Бэкап не запущен: нет сигнала от бота")]

    stale = age(heartbeat.get("ts"), now)
    if stale is None or stale > STALE_AFTER:
        problems.append(Problem("stale", f"🚨 Бэкап не отвечает уже {human(stale or 0)} — бот упал или завис"))
    elif heartbeat.get("status") == "unauthorized":
        problems.append(Problem("auth", "🚨 Сеанс Telegram завершён — бэкап НЕ работает, нужен повторный вход"))
    else:
        offline = age(heartbeat.get("disconnected_since"), now)
        if offline is not None and offline > OFFLINE_AFTER:
            problems.append(Problem("offline", f"⚠️ Нет связи с Telegram уже {human(offline)}"))
        if heartbeat.get("bot_connected") is False:
            problems.append(Problem("notifier", "⚠️ Бот-уведомитель отключён — уведомления могут не приходить"))

    started = heartbeat.get("started_at")
    if started and started != state.last_started_at:
        if state.last_started_at is not None:
            state.starts.append(now)
        state.last_started_at = started
    recent = [t for t in state.starts if now - t < 3600]
    if len(recent) >= RESTART_LIMIT:
        problems.append(Problem("restarts", f"⚠️ Бот перезапускался {len(recent)} раз за час — смотрите логи"))

    uptime = age(started, now) or 0
    anchor_age = age(heartbeat.get("last_anchor_at"), now)
    if uptime > ANCHOR_MAX_AGE and (anchor_age is None or anchor_age > ANCHOR_MAX_AGE):
        problems.append(Problem("anchor", "⚠️ Метка времени не ставилась больше 36 ч — проверьте /anchor"))
    elif heartbeat.get("last_anchor_tsa") is False and anchor_age is not None and anchor_age < ANCHOR_MAX_AGE:
        problems.append(Problem("tsa", "⚠️ Последняя метка RFC 3161 (FreeTSA) не получена, повтор будет автоматически"))

    usage = shutil.disk_usage(data_dir)
    used = usage.used / usage.total
    if used > DISK_LIMIT:
        problems.append(Problem("disk", f"⚠️ Диск заполнен на {used:.0%}, свободно {usage.free / 1024 ** 3:.1f} ГБ"))
    return problems


def transitions(problems: list[Problem], state: State, now: float) -> list[str]:
    messages = []
    current = {p.key: p for p in problems}
    for key, problem in current.items():
        last = state.active.get(key)
        if last is None or now - last >= REMIND_EVERY:
            prefix = "" if last is None else "🔁 Всё ещё: "
            messages.append(prefix + problem.text)
            state.active[key] = now
    for key in list(state.active):
        if key not in current:
            del state.active[key]
            messages.append(RECOVERED.get(key, "✅ Проблема устранена"))
    return messages


RECOVERED = {
    "stale": "✅ Бэкап снова работает",
    "auth": "✅ Сеанс Telegram восстановлен",
    "offline": "✅ Связь с Telegram восстановлена",
    "notifier": "✅ Бот-уведомитель снова на связи",
    "restarts": "✅ Перезапуски прекратились",
    "anchor": "✅ Метка времени поставлена",
    "tsa": "✅ Метка RFC 3161 получена",
    "disk": "✅ Место на диске в норме",
}


def send(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": f"🐕 <b>Сторож бэкапа</b>\n{text}", "parse_mode": "HTML"}
    ).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def ping(url: str, fail: bool) -> None:
    try:
        urllib.request.urlopen(url + ("/fail" if fail else ""), timeout=15).read()
    except Exception as exc:
        log.warning("Healthcheck ping failed: %s", exc)


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def main() -> int:
    load_dotenv(os.getenv("ENV_FILE", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    token = os.getenv("NOTIFY_BOT_TOKEN", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()
    if not token or not owner:
        log.error("NOTIFY_BOT_TOKEN and OWNER_ID are required for the watchdog")
        return 1
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    heartbeat_path = data_dir / "heartbeat.json"
    ping_url = os.getenv("HEALTHCHECK_URL", "").strip()

    state = State()
    time.sleep(STARTUP_GRACE)
    log.info("Watchdog started")
    while True:
        now = time.time()
        try:
            problems = evaluate(read_heartbeat(heartbeat_path), data_dir, state, now)
            for text in transitions(problems, state, now):
                log.warning(text)
                try:
                    send(token, owner, text)
                except Exception as exc:
                    log.error("Cannot send alert: %s", exc)
            if ping_url:
                ping(ping_url, fail=any(p.key in {"stale", "auth", "offline"} for p in problems))
        except Exception:
            log.exception("Watchdog check failed")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
