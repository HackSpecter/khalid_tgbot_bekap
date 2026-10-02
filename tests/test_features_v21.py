import asyncio
import io
import json
import zipfile
from dataclasses import replace

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession

from tgbackup import keyboards
from tgbackup.commands import Commands
from tgbackup.db import Database, MessageRecord
from tgbackup.deletions import DeletionAggregator
from tgbackup.evidence import ChainVerifier, EvidenceLog
from tgbackup.handlers import Tracker
from tgbackup.media import MediaDownloader
from tgbackup.notifier import Notifier
from tgbackup.settings import Settings

CHAT = 42


@pytest.fixture
def client():
    return TelegramClient(StringSession(), 1, "h")


@pytest.fixture
async def settings(config, db):
    return await Settings(config, db).load()


@pytest.fixture
def tracker(config, db, evidence, vault, exporter, notifier, client, settings):
    downloader = MediaDownloader(client, vault, config.media_max_bytes)
    deletions = DeletionAggregator(config, db, exporter, notifier, evidence, settings)
    tracker = Tracker(config, db, downloader, evidence, notifier, deletions, None, settings)
    tracker.me_id = 1
    return tracker


@pytest.fixture
def commands(config, db, exporter, notifier, client, tracker, settings):
    return Commands(config, db, exporter, notifier, client, tracker, None, settings)


async def seed(db, evidence, count=3, outgoing=True):
    await db.upsert_chat(CHAT, "Ali", "private", "ali")
    for i in range(1, count + 1):
        record = MessageRecord(CHAT, i, f"2026-10-01T09:00:0{i}+00:00", sender_id=1 if outgoing else CHAT,
                               outgoing=outgoing, text=f"msg {i}")
        await db.save_message(record)
        entry = await evidence.append("new", chat_id=CHAT, message_id=i, raw={"m": i})
        await db.link_evidence(CHAT, i, entry.seq, entry.record_hash)


async def test_two_processes_cannot_fork_chain(config, db):
    other = Database(config.db_path)
    await other.connect()
    try:
        first, second = EvidenceLog(db), EvidenceLog(other)
        await asyncio.gather(
            *[first.append("new", chat_id=1, message_id=i) for i in range(25)],
            *[second.append("new", chat_id=2, message_id=i) for i in range(25)],
        )
        verifier = ChainVerifier()
        async for entry in first.iter_all():
            verifier.feed(entry)
        assert verifier.count == 50 and not verifier.issues
    finally:
        await other.close()


async def test_database_rejects_stale_head(db, evidence):
    await evidence.append("new", chat_id=1, message_id=1)
    with pytest.raises(Exception, match="evidence chain broken"):
        await db.conn.execute(
            "INSERT INTO evidence_log (seq, event, observed_at, payload, record_hash, prev_hash, entry_hash) "
            "VALUES (2, 'new', 'x', '{}', 'r', ?, 'e')",
            ("0" * 64,),
        )


async def test_settings_persist_and_apply(config, db, tracker):
    settings = tracker.settings
    assert settings.get("track_groups") is False
    assert await settings.toggle("track_groups") is True
    await settings.set_muted(CHAT, True)
    await settings.set_excluded(-100, True)

    reloaded = await Settings(config, db).load()
    assert reloaded.get("track_groups") is True
    assert reloaded.is_muted(CHAT) and reloaded.is_excluded(-100)
    assert tracker.kind_enabled("group") and tracker.is_ignored_chat(-100)


async def test_muted_chat_gets_no_deletion_notifications(db, evidence, tracker, notifier):
    await seed(db, evidence, outgoing=False)
    await tracker.settings.set_muted(CHAT, True)
    await tracker.deletions.add(None, [1])
    await tracker.deletions.flush()
    assert notifier.texts == [] and (await db.get_message(CHAT, 1))["deleted"] == 1


async def test_read_receipts_logged_once(db, evidence, tracker, exporter):
    await seed(db, evidence)
    assert await tracker.apply_read(CHAT, 2, raw={"_": "UpdateReadHistoryOutbox", "max_id": 2}) == 2
    assert await tracker.apply_read(CHAT, 2) == 0
    assert await tracker.apply_read(CHAT, 3) == 1

    rows = await db.chat_messages(CHAT)
    assert all(row["read_at"] for row in rows)
    reads = [e for e in await evidence.for_chat(CHAT) if e.event == "read"]
    assert [json.loads(e.payload)["data"]["message_ids"] for e in reads] == [[1, 2], [3]]

    parts = await exporter.export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        html = archive.read("chat.html").decode()
        manifest = json.loads(archive.read("manifest.json"))
        events = [json.loads(line)["event"] for line in archive.read("evidence/records.jsonl").decode().splitlines()]
    assert html.count("✓✓") == 3 and "прочитано ≤" in html
    assert all(m["read_not_later_than_utc"] for m in manifest["messages"])
    assert events.count("read") == 2


def flat(buttons, attr):
    return [b.type.data if attr == "data" else b.text for row in buttons for b in row]


async def test_callbacks(commands, db, evidence, notifier, settings):
    await seed(db, evidence)
    result = await commands.handle_callback(keyboards.MUTE, str(CHAT))
    assert settings.is_muted(CHAT) and result.edit_buttons_only
    assert flat(result.buttons, "data") == [keyboards.data(keyboards.UNMUTE, CHAT)]

    result = await commands.handle_callback(keyboards.MENU, "muted")
    assert "Заглушённые" in result.text and b"v:42" in flat(result.buttons, "data")
    result = await commands.handle_callback(keyboards.LIST_UNMUTE, str(CHAT))
    assert not settings.is_muted(CHAT) and "Список пуст" in result.text

    result = await commands.handle_callback(keyboards.TOGGLE, "track_channels")
    assert settings.get("track_channels") and "Каналы: вкл" in result.toast
    assert "✅ Каналы" in flat(result.buttons, "text")

    await settings.set_excluded(CHAT, True)
    result = await commands.handle_callback(keyboards.INCLUDE, str(CHAT))
    assert not settings.is_excluded(CHAT)

    await commands.handle_callback(keyboards.EXPORT, str(CHAT))
    await asyncio.gather(*commands._background)
    assert notifier.exports and notifier.exports[0][1] == "Ali (@ali)"


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def send_file(self, target, file, **kwargs):
        name = file.name if isinstance(file, io.BytesIO) else file
        self.calls.append((str(name), kwargs.get("caption"), kwargs.get("buttons")))

    async def send_message(self, target, text, **kwargs):
        self.calls.append(("text", text, kwargs.get("buttons")))


async def test_send_export_pdf_first(tmp_path):
    archive_path = tmp_path / "chat_1.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("chat.pdf", b"%PDF-1.7")
        archive.writestr("chat.html", "<html>")
    client = RecordingClient()
    notifier = Notifier(client, 1, supports_buttons=True)
    await notifier.send_export([archive_path], "Ali", "⚠️ intro", buttons=keyboards.chat_actions(CHAT))

    assert [call[0] for call in client.calls] == ["chat_1.pdf", str(archive_path)]
    assert "intro" in client.calls[0][1] and "читать" in client.calls[0][1]
    assert "доказательствами" in client.calls[1][1] and client.calls[1][2] is not None


async def test_saved_messages_mode_drops_buttons():
    client = RecordingClient()
    notifier = Notifier(client, "me")
    await notifier.send_text("hi", buttons=keyboards.chat_actions(CHAT))
    assert client.calls == [("text", "hi", None)]
