from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

from .config import Config
from .db import Database
from .evidence import EVENT_PROFILE, EvidenceLog, canonical_json, raw_dict
from .cards import profile_card
from .formatting import full_name
from .keyboards import chat_actions
from .media import MediaDownloader
from .notifier import Notifier
from .settings import Settings

log = logging.getLogger(__name__)

FIELDS = ("first_name", "last_name", "username", "phone", "bio", "photo_id")
LABELS = {
    "first_name": "Имя",
    "last_name": "Фамилия",
    "username": "Username",
    "phone": "Телефон",
    "bio": "О себе",
    "photo_id": "Фото профиля",
}
UNKNOWN = object()


def primary_username(user: types.User) -> str | None:
    if user.username:
        return user.username
    for item in user.usernames or []:
        if item.active:
            return item.username
    return None


def photo_id_of(user: types.User) -> int | None:
    photo = user.photo
    return photo.photo_id if isinstance(photo, types.UserProfilePhoto) else None


class ProfileTracker:
    def __init__(
        self,
        config: Config,
        db: Database,
        evidence: EvidenceLog,
        downloader: MediaDownloader,
        notifier: Notifier | None,
        client: TelegramClient,
        settings: Settings | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or Settings(config)
        self.db = db
        self.evidence = evidence
        self.downloader = downloader
        self.notifier = notifier
        self.client = client
        self.me_id: int | None = None
        self._cache: dict[int, dict] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _last(self, user_id: int) -> dict | None:
        if user_id not in self._cache:
            row = await self.db.last_profile(user_id)
            if row is None:
                return None
            self._cache[user_id] = {field: row[field] for field in FIELDS}
        return self._cache[user_id]

    async def observe(
        self,
        user: types.User,
        *,
        bio: object = UNKNOWN,
        source: str = "event",
        full: object | None = None,
    ) -> dict | None:
        if not isinstance(user, types.User) or user.min or user.id == self.me_id:
            return None
        async with self._locks[user.id]:
            previous = await self._last(user.id)
            current = {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": primary_username(user),
                "phone": user.phone,
                "bio": None if bio is UNKNOWN else (bio or None),
                "photo_id": photo_id_of(user),
            }
            if previous is not None:
                if current["phone"] is None:
                    current["phone"] = previous["phone"]
                if bio is UNKNOWN:
                    current["bio"] = previous["bio"]

            if previous is None:
                changes = {field: [None, value] for field, value in current.items() if value is not None}
            else:
                changes = {
                    field: [previous[field], current[field]]
                    for field in FIELDS
                    if previous[field] != current[field]
                }
                if not changes:
                    return None

            stored = None
            if current["photo_id"] and (previous is None or "photo_id" in changes):
                stored = await self.downloader.download_profile_photo(user, current["photo_id"])

            entry = await self.evidence.append(
                EVENT_PROFILE,
                chat_id=user.id,
                raw=raw_dict(user),
                media_sha256=stored.sha256 if stored else None,
                media_path=stored.path if stored else None,
                data={
                    "source": source,
                    "initial": previous is None,
                    "changes": changes,
                    "snapshot": current,
                    "full_user": raw_dict(full) if full is not None else None,
                },
            )
            await self.db.add_profile_snapshot(
                {
                    "user_id": user.id,
                    "observed_at": entry.observed_at,
                    "source": source,
                    **current,
                    "photo_path": stored.path if stored else None,
                    "photo_sha256": stored.sha256 if stored else None,
                    "changes": canonical_json(changes),
                    "evidence_seq": entry.seq,
                }
            )
            self._cache[user.id] = current

        if previous is not None and self.notifier is not None and not self.settings.is_muted(user.id):
            await self.notify(user, changes, stored.path if stored and stored.path else None)
        return changes

    async def notify(self, user: types.User, changes: dict, photo_path: str | None) -> None:
        name = full_name(user.first_name, user.last_name) or str(user.id)
        rows = []
        for field_name, (old, new) in changes.items():
            if field_name == "photo_id":
                rows.append((LABELS[field_name], "старое" if old else None, "новое" if new else None))
            elif field_name == "username":
                rows.append((LABELS[field_name], f"@{old}" if old else None, f"@{new}" if new else None))
            else:
                rows.append((LABELS[field_name], old, new))
        text = profile_card(user.id, name, rows)
        buttons = chat_actions(user.id)
        if photo_path:
            await self.notifier.send_media(photo_path, text, buttons=buttons)
        else:
            await self.notifier.send_text(text, buttons=buttons)

    async def sweep(self) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=self.config.profile_active_days)).isoformat()
        user_ids = await self.db.active_private_users(since)
        checked = 0
        for user_id in user_ids:
            if user_id == self.me_id or user_id == self.config.notifier_bot_id:
                continue
            try:
                result = await self.client(functions.users.GetFullUserRequest(user_id))
            except FloodWaitError as exc:
                log.warning("Profile sweep flood wait %ss", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except Exception as exc:
                log.warning("Profile sweep: cannot load %s: %s", user_id, exc)
                continue
            user = next((u for u in result.users if u.id == user_id), None)
            if user is not None:
                try:
                    await self.observe(user, bio=result.full_user.about or "", source="daily", full=result.full_user)
                    checked += 1
                except Exception:
                    log.exception("Profile sweep failed for %s", user_id)
            await asyncio.sleep(self.config.profile_sweep_pause)
        log.info("Profile sweep: %s users checked", checked)
        return checked


    async def run_scheduler(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                local = datetime.now(timezone.utc).astimezone(self.config.timezone)
                last = await self.db.kv_get("profile_sweep_date")
                if local.hour >= self.config.profile_sweep_hour and last != local.date().isoformat():
                    await self.sweep()
                    await self.db.kv_set("profile_sweep_date", local.date().isoformat())
            except Exception:
                log.exception("Profile sweep scheduler failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass
