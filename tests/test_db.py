import pytest

from tgbackup.db import MessageRecord

PRIVATE = 111
GROUP = -222
CHANNEL = -1001234567890


def message(chat_id, message_id, text="hi", **kwargs):
    return MessageRecord(chat_id=chat_id, message_id=message_id, date="2026-10-01T10:00:00+00:00", text=text, **kwargs)


async def test_wal_mode(db):
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"


async def test_save_and_upsert_keeps_media_path(db):
    await db.save_message(message(PRIVATE, 1, media_type="photo", media_uid="photo:1"))
    await db.set_media(PRIVATE, 1, "media/111/1_1.jpg", 100, None)
    await db.save_message(message(PRIVATE, 1, text="updated", media_type="photo", media_uid="photo:1"))

    row = await db.get_message(PRIVATE, 1)
    assert row["text"] == "updated"
    assert row["media_path"] == "media/111/1_1.jpg"
    assert row["media_size"] == 100


async def test_deletion_without_chat_id_skips_channels(db):
    await db.save_message(message(PRIVATE, 10))
    await db.save_message(message(CHANNEL, 10))

    rows = await db.find_for_deletion([10], None)
    assert [r["chat_id"] for r in rows] == [PRIVATE]


async def test_deletion_with_chat_id(db):
    await db.save_message(message(PRIVATE, 10))
    await db.save_message(message(CHANNEL, 10))

    rows = await db.find_for_deletion([10, 11], CHANNEL)
    assert [r["chat_id"] for r in rows] == [CHANNEL]


async def test_mark_deleted_is_soft_and_idempotent(db):
    await db.save_message(message(PRIVATE, 5))
    rows = await db.find_for_deletion([5], None)
    await db.mark_deleted([r["id"] for r in rows], "2026-10-02T00:00:00+00:00")

    row = await db.get_message(PRIVATE, 5)
    assert row["deleted"] == 1
    assert row["deleted_at"] == "2026-10-02T00:00:00+00:00"
    assert row["text"] == "hi"
    assert await db.find_for_deletion([5], None) == []


async def test_record_edit(db):
    await db.save_message(message(PRIVATE, 7, text="old"))
    old = await db.get_message(PRIVATE, 7)
    new = message(PRIVATE, 7, text="new", edited=True, edit_date="2026-10-01T11:00:00+00:00")
    await db.record_edit(old, new)
    await db.save_message(new)

    edits = await db.chat_edits(PRIVATE)
    assert [(e["old_text"], e["new_text"]) for e in edits[7]] == [("old", "new")]
    row = await db.get_message(PRIVATE, 7)
    assert row["edited"] == 1 and row["text"] == "new"


async def test_upsert_does_not_reset_edited_flag(db):
    await db.save_message(message(PRIVATE, 8, edited=True, edit_date="2026-10-01T11:00:00+00:00"))
    await db.save_message(message(PRIVATE, 8))
    row = await db.get_message(PRIVATE, 8)
    assert row["edited"] == 1
    assert row["edit_date"] == "2026-10-01T11:00:00+00:00"


async def test_search_is_case_insensitive_for_cyrillic(db):
    await db.upsert_chat(PRIVATE, "Друг", "private", None)
    await db.save_message(message(PRIVATE, 1, text="Привет, как ДЕЛА?"))
    await db.save_message(message(PRIVATE, 2, text="100% готово"))

    assert [r["message_id"] for r in await db.search("дела")] == [1]
    assert [r["message_id"] for r in await db.search("100%")] == [2]
    assert await db.search("0_") == []


@pytest.mark.parametrize("query", ["111", "@friend", "FRIEND", "друг"])
async def test_find_chat(db, query):
    await db.upsert_chat(PRIVATE, "Друг Дружище", "private", None)
    await db.upsert_user(PRIVATE, "Друг", "Дружище", "Friend", None, False)
    chat = await db.find_chat(query)
    assert chat is not None and chat["id"] == PRIVATE


async def test_stats(db):
    await db.upsert_chat(PRIVATE, "a", "private", None)
    await db.save_message(message(PRIVATE, 1, media_type="photo"))
    await db.set_media(PRIVATE, 1, "media/x.jpg", 1, None)
    await db.save_message(message(GROUP, 2))
    stats = await db.stats()
    assert stats.messages == 2
    assert stats.chats == 1
    assert stats.media_files == 1
    assert stats.db_bytes > 0
