from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _id_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{name}: {chunk!r} is not a chat id") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    data_dir: Path
    notify_bot_token: str | None
    owner_id: int | None
    track_private: bool = True
    track_groups: bool = False
    track_channels: bool = False
    excluded_chat_ids: frozenset[int] = field(default_factory=frozenset)
    media_max_size_mb: int = 50
    bulk_delete_threshold: int = 10
    bulk_delete_window_sec: int = 30
    notify_own_actions: bool = False
    export_part_size_mb: int = 49
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    log_level: str = "INFO"

    @property
    def session_path(self) -> Path:
        return self.data_dir / self.session_name

    @property
    def notifier_session_path(self) -> Path:
        return self.data_dir / f"{self.session_name}_notifier"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "backup.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def media_max_bytes(self) -> int:
        return self.media_max_size_mb * 1024 * 1024

    @property
    def export_part_bytes(self) -> int:
        return self.export_part_size_mb * 1024 * 1024

    @property
    def notifier_bot_id(self) -> int | None:
        if not self.notify_bot_token:
            return None
        head = self.notify_bot_token.split(":", 1)[0]
        return int(head) if head.isdigit() else None

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.media_dir, self.exports_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    load_dotenv(env_file or os.getenv("ENV_FILE", ".env"))

    api_id = _int("API_ID")
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise ConfigError("API_ID and API_HASH are required (https://my.telegram.org)")

    token = os.getenv("NOTIFY_BOT_TOKEN", "").strip() or None
    owner_id = _int("OWNER_ID")
    if token and not owner_id:
        raise ConfigError("OWNER_ID is required when NOTIFY_BOT_TOKEN is set")

    tz_name = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise ConfigError(f"Unknown TIMEZONE {tz_name!r}") from exc

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=os.getenv("SESSION_NAME", "userbot").strip() or "userbot",
        data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
        notify_bot_token=token,
        owner_id=owner_id,
        track_private=_bool("TRACK_PRIVATE", True),
        track_groups=_bool("TRACK_GROUPS", False),
        track_channels=_bool("TRACK_CHANNELS", False),
        excluded_chat_ids=_id_set("EXCLUDED_CHAT_IDS"),
        media_max_size_mb=_int("MEDIA_MAX_SIZE_MB", 50),
        bulk_delete_threshold=max(1, _int("BULK_DELETE_THRESHOLD", 10)),
        bulk_delete_window_sec=max(1, _int("BULK_DELETE_WINDOW_SEC", 30)),
        notify_own_actions=_bool("NOTIFY_OWN_ACTIONS", False),
        export_part_size_mb=max(1, _int("EXPORT_PART_SIZE_MB", 49)),
        timezone=tz,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def setup_logging(config: Config) -> None:
    config.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        config.logs_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.log_level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    logging.getLogger("telethon").setLevel(logging.WARNING)
