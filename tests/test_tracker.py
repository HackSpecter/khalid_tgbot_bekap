import datetime as dt
import json

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types

from tgbackup.deletions import DeletionAggregator
from tgbackup.evidence import ChainVerifier
from tgbackup.handlers import Tracker
from tgbackup.media import MediaDownloader
from tgbackup.profiles import ProfileTracker

UTC = dt.timezone.utc


class Event:
    def __init__(self, message):
        self.message = message
        self.chat_id = message.chat_id
        self.id = message.id


class DeleteEvent:
    chat_id = None
    original_update = types.UpdateDeleteMessages(messages=[1, 4], pts=1, pts_count=1)

    def __init__(self, ids):
        self.deleted_ids = ids


@pytest.fixture
def client():
    return TelegramClient(StringSession(), 1, "h")


@pytest.fixture
def user():
    return types.User(id=42, first_name="Ali", last_name="V", username="ali", access_hash=1, phone="992111")


@pytest.fixture
def tracker(config, db, evidence, vault, exporter, notifier, client):
    downloader = MediaDownloader(client, vault, config.media_max_bytes)
    deletions = DeletionAggregator(config, db, exporter, notifier, evidence)
    profiles = ProfileTracker(config, db, evidence, downloader, notifier, client)
    tracker = Tracker(config, db, downloader, evidence, notifier, deletions, profiles)
    tracker.client = client
    tracker.me_id = profiles.me_id = 1
    return tracker


def message(client, user, mid, text, **kwargs):
    msg = types.Message(id=mid, peer_id=types.PeerUser(42), date=dt.datetime(2026, 10, 1, tzinfo=UTC),
                        message=text, out=False, from_id=None, **kwargs)
    msg._finish_init(client, {42: user}, None)
    return msg


async def test_full_lifecycle_is_chained(tracker, client, user, db, evidence, notifier):
    await tracker.on_new(Event(message(client, user, 1, "hello")))
    await tracker.on_new(Event(message(
        client, user, 2, "", fwd_from=types.MessageFwdHeader(date=dt.datetime(2026, 9, 1, tzinfo=UTC), from_name="Канал"),
    )))
    await tracker.on_edit(Event(message(client, user, 1, "hello edited", edit_date=dt.datetime(2026, 10, 1, 0, 5, tzinfo=UTC))))
    await tracker.on_edit(Event(message(client, user, 1, "hello edited", edit_date=dt.datetime(2026, 10, 1, 0, 6, tzinfo=UTC))))
    reactions = types.MessageReactions(results=[types.ReactionCount(reaction=types.ReactionEmoji("❤"), count=1)])
    await tracker.on_reactions(types.UpdateMessageReactions(peer=types.PeerUser(42), msg_id=1, reactions=reactions))

    call = types.MessageService(
        id=3, peer_id=types.PeerUser(42), date=dt.datetime(2026, 10, 1, 1, tzinfo=UTC), out=False,
        action=types.MessageActionPhoneCall(call_id=9, video=False, reason=types.PhoneCallDiscardReasonMissed()),
    )
    await tracker.on_service(types.UpdateNewMessage(message=call, pts=1, pts_count=1))

    await tracker.on_delete(DeleteEvent([1]))
    await tracker.deletions.flush()

    events = [entry.event async for entry in evidence.iter_all()]
    assert events == ["profile_change", "new", "new", "edit", "reaction", "new", "delete"]

    verifier = ChainVerifier()
    async for entry in evidence.iter_all():
        verifier.feed(entry)
    assert not verifier.issues

    row1 = await db.get_message(42, 1)
    assert row1["text"] == "hello edited" and row1["deleted"] == 1 and row1["edited"] == 1
    assert json.loads(row1["reactions"])["counts"] == [{"n": 1, "r": "❤"}]
    row2 = await db.get_message(42, 2)
    assert row2["fwd_from_name"] == "Канал"
    row3 = await db.get_message(42, 3)
    assert row3["service_action"] == "📞 Звонок (входящий), пропущен"
    assert row3["first_seq"] is not None

    new_entry = await evidence.entry_at(row1["first_seq"])
    assert json.loads(new_entry.payload)["raw"]["message"] == "hello"
    assert any("<b>Было</b>" in text and "<blockquote>hello edited</blockquote>" in text for text in notifier.texts)
    assert any("Удалено сообщение" in text for text in notifier.texts)


async def test_profile_change_detected_and_notified(tracker, client, user, db, notifier):
    await tracker.on_new(Event(message(client, user, 1, "a")))
    renamed = types.User(id=42, first_name="Alisher", last_name="V", username="alisher", access_hash=1)
    await tracker.on_new(Event(message(client, renamed, 2, "b")))

    history = await db.profile_history(42)
    assert len(history) == 2
    changes = json.loads(history[1]["changes"])
    assert changes == {"first_name": ["Ali", "Alisher"], "username": ["ali", "alisher"]}
    assert history[1]["phone"] == "992111"
    assert any("Профиль изменён" in text and "<s>Ali</s> → <b>Alisher</b>" in text for text in notifier.texts)


async def test_view_once_media_is_captured(tracker, client, user, db, evidence, notifier, vault):
    async def fake_download(media, file=None, **kwargs):
        return b"secret-view-once-bytes"

    tracker.downloader.client.download_media = fake_download
    photo = types.MessageMediaPhoto(
        photo=types.Photo(id=9, access_hash=1, file_reference=b"", date=None,
                          sizes=[types.PhotoSize("x", 1, 1, 100)], dc_id=2),
        ttl_seconds=10,
    )
    await tracker.on_new(Event(message(client, user, 5, "", media=photo)))

    row = await db.get_message(42, 5)
    assert row["view_once"] == 1
    assert row["media_path"] is not None
    assert vault.read(row["media_path"]) == b"secret-view-once-bytes"

    entry = await evidence.entry_at(row["first_seq"])
    assert json.loads(entry.payload)["data"]["view_once"] is True
    assert any("View-once перехвачено" in text for text in notifier.texts) or any(
        "View-once перехвачено" in caption for _, caption, _ in notifier.media
    )
