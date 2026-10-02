from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
STATUS_OK = "ok"
STATUS_UNAUTHORIZED = "unauthorized"
STATUS_STOPPING = "stopping"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Health:
    path: Path
    started_at: str = field(default_factory=now_iso)
    status: str = STATUS_OK
    user_connected: bool = False
    bot_connected: bool | None = None
    disconnected_since: str | None = None
    last_anchor_at: str | None = None
    last_anchor_tsa: bool | None = None
    account: str | None = None

    def mark_connection(self, connected: bool) -> None:
        if connected:
            self.disconnected_since = None
        elif self.user_connected or self.disconnected_since is None:
            self.disconnected_since = now_iso()
        self.user_connected = connected

    def write(self) -> None:
        data = asdict(self)
        data.pop("path")
        data.update({"ts": now_iso(), "pid": os.getpid(), "version": __version__})
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, self.path)


async def run_heartbeat(health: Health, client, bot, db, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            health.mark_connection(client.is_connected())
            health.bot_connected = bot.is_connected() if bot is not None else None
            if db is not None:
                anchor = await db.last_anchor()
                if anchor is not None:
                    health.last_anchor_at = anchor["created_at"]
                    health.last_anchor_tsa = bool(anchor["tsr_file"])
            health.write()
        except Exception:
            log.exception("Heartbeat failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass
