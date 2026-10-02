from __future__ import annotations

import json

from .config import Config

TOGGLES = {
    "track_private": "Личные чаты",
    "track_groups": "Группы",
    "track_channels": "Каналы",
    "notify_own_actions": "Уведомлять о моих правках/удалениях",
}
KV_KEY = "settings"


class Settings:
    def __init__(self, config: Config, db=None) -> None:
        self.config = config
        self.db = db
        self.overrides: dict[str, bool] = {}
        self.excluded: set[int] = set(config.excluded_chat_ids)
        self.muted: set[int] = set()

    async def load(self) -> "Settings":
        if self.db is None:
            return self
        raw = await self.db.kv_get(KV_KEY)
        if raw is None:
            await self.save()
            return self
        data = json.loads(raw)
        self.overrides = {key: bool(value) for key, value in data.get("overrides", {}).items() if key in TOGGLES}
        self.excluded = {int(chat_id) for chat_id in data.get("excluded", [])}
        self.muted = {int(chat_id) for chat_id in data.get("muted", [])}
        return self

    async def save(self) -> None:
        if self.db is None:
            return
        data = {"overrides": self.overrides, "excluded": sorted(self.excluded), "muted": sorted(self.muted)}
        await self.db.kv_set(KV_KEY, json.dumps(data))

    def get(self, key: str) -> bool:
        if key in self.overrides:
            return self.overrides[key]
        return bool(getattr(self.config, key))

    async def toggle(self, key: str) -> bool:
        if key not in TOGGLES:
            raise KeyError(key)
        self.overrides[key] = not self.get(key)
        await self.save()
        return self.overrides[key]

    def is_excluded(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.excluded

    def is_muted(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.muted

    async def set_muted(self, chat_id: int, muted: bool) -> None:
        (self.muted.add if muted else self.muted.discard)(chat_id)
        await self.save()

    async def set_excluded(self, chat_id: int, excluded: bool) -> None:
        (self.excluded.add if excluded else self.excluded.discard)(chat_id)
        await self.save()
