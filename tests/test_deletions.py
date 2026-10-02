import json
import zipfile
from dataclasses import replace

from tgbackup.db import MessageRecord
from tgbackup.deletions import DeletionAggregator
from tgbackup.evidence import EVENT_DELETE


async def seed(db, evidence, chat_id, count, outgoing=False):
    await db.upsert_chat(chat_id, f"chat{chat_id}", "private", None)
    for i in range(1, count + 1):
        record = MessageRecord(chat_id, chat_id * 1000 + i, f"2026-10-01T09:00:{i:02d}+00:00",
                               sender_id=chat_id, sender_name="Петя", text=f"msg {i}", outgoing=outgoing)
        await db.save_message(record)
        entry = await evidence.append("new", chat_id=chat_id, message_id=record.message_id, raw={"text": record.text})
        await db.link_evidence(chat_id, record.message_id, entry.seq, entry.record_hash)


def aggregator(config, db, exporter, notifier, evidence, **overrides):
    return DeletionAggregator(replace(config, **overrides), db, exporter, notifier, evidence)


async def test_small_deletion_notifies_each(db, config, exporter, notifier, evidence):
    agg = aggregator(config, db, exporter, notifier, evidence, bulk_delete_threshold=5)
    await seed(db, evidence, 7, 3)

    assert await agg.add(None, [7001, 7002], {"_": "UpdateDeleteMessages"}) == 2
    assert (await db.get_message(7, 7001))["deleted"] == 1
    await agg.flush()

    assert len(notifier.texts) == 2
    assert "msg 1" in notifier.texts[0] and "Петя" in notifier.texts[0]
    assert (await db.get_message(7, 7003))["deleted"] == 0


async def test_deletion_is_logged_on_receipt(db, config, exporter, notifier, evidence):
    agg = aggregator(config, db, exporter, notifier, evidence)
    await seed(db, evidence, 7, 2)
    first = await db.get_message(7, 7001)

    await agg.add(None, [7001, 999999], {"_": "UpdateDeleteMessages", "messages": [7001, 999999]})

    entries = await evidence.for_chat(7)
    delete = entries[-1]
    assert delete.event == EVENT_DELETE
    payload = json.loads(delete.payload)
    assert payload["data"]["message_ids"] == [7001]
    assert payload["data"]["prior_record_hashes"] == {"7001": first["record_hash"]}
    assert payload["raw"]["messages"] == [7001, 999999]


async def test_bulk_deletion_sends_export(db, config, exporter, notifier, evidence):
    agg = aggregator(config, db, exporter, notifier, evidence, bulk_delete_threshold=5)
    await seed(db, evidence, 8, 6)
    await seed(db, evidence, 9, 1)

    await agg.add(None, [8001, 8002, 8003])
    await agg.add(None, [8004, 8005, 8006, 9001])
    await agg.flush()

    assert len(notifier.exports) == 1
    parts, title, intro = notifier.exports[0]
    assert "был удалён/очищен" in intro and "chat8" in intro and title == "chat8"
    with zipfile.ZipFile(parts[0]) as archive:
        assert archive.read("chat.html").decode().count("msg ") == 6
        assert "manifest.json" in archive.namelist()
    assert len(notifier.texts) == 1 and "chat9" in notifier.texts[0]


async def test_own_deletions_are_silent_by_default(db, config, exporter, notifier, evidence):
    agg = aggregator(config, db, exporter, notifier, evidence)
    await seed(db, evidence, 4, 1, outgoing=True)

    await agg.add(None, [4001])
    await agg.flush()

    assert notifier.texts == [] and notifier.files == [] and notifier.media == []
    assert (await db.get_message(4, 4001))["deleted"] == 1


async def test_media_sent_with_deleted_message(db, config, exporter, notifier, evidence, vault):
    agg = aggregator(config, db, exporter, notifier, evidence)
    await db.upsert_chat(3, "c", "private", None)
    rel, digest, size = vault.write("media/3/1_1.jpg", b"jpg")
    await db.save_message(MessageRecord(3, 1, "2026-10-01T09:00:00+00:00", sender_id=3, text="смотри",
                                        media_type="photo", media_uid="photo:1", media_path=rel))

    await agg.add(None, [1])
    await agg.flush()

    assert notifier.media and notifier.media[0][0] == rel
    assert "смотри" in notifier.media[0][1] and notifier.media[0][2] == "photo"
