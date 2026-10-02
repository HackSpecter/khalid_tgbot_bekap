import zipfile
from dataclasses import replace

from tgbackup.db import MessageRecord
from tgbackup.deletions import DeletionAggregator
from tgbackup.exporter import Exporter


class FakeNotifier:
    def __init__(self):
        self.texts = []
        self.files = []

    async def send_text(self, text):
        self.texts.append(text)

    async def send_file(self, path, caption="", *, as_document=False):
        self.files.append((path, caption))


async def seed(db, chat_id, count, outgoing=False):
    await db.upsert_chat(chat_id, f"chat{chat_id}", "private", None)
    for i in range(1, count + 1):
        await db.save_message(
            MessageRecord(chat_id, chat_id * 1000 + i, f"2026-10-01T09:00:{i:02d}+00:00",
                          sender_id=chat_id, sender_name="Петя", text=f"msg {i}", outgoing=outgoing)
        )


async def test_small_deletion_notifies_each(db, config):
    notifier = FakeNotifier()
    agg = DeletionAggregator(replace(config, bulk_delete_threshold=5), db, Exporter(db, config), notifier)
    await seed(db, 7, 3)

    await agg.add(None, [7001, 7002])
    await agg.flush()

    assert len(notifier.texts) == 2
    assert "msg 1" in notifier.texts[0] and "Петя" in notifier.texts[0]
    assert (await db.get_message(7, 7001))["deleted"] == 1
    assert (await db.get_message(7, 7003))["deleted"] == 0


async def test_bulk_deletion_sends_export(db, config):
    notifier = FakeNotifier()
    agg = DeletionAggregator(replace(config, bulk_delete_threshold=5), db, Exporter(db, config), notifier)
    await seed(db, 8, 6)
    await seed(db, 9, 1)

    await agg.add(None, [8001, 8002, 8003])
    await agg.add(None, [8004, 8005, 8006, 9001])
    await agg.flush()

    assert len(notifier.files) == 1
    path, caption = notifier.files[0]
    assert "был удалён/очищен" in caption and "chat8" in caption
    with zipfile.ZipFile(path) as archive:
        assert archive.read("chat.html").decode().count("msg ") == 6
    assert len(notifier.texts) == 1 and "chat9" in notifier.texts[0]


async def test_own_deletions_are_silent_by_default(db, config):
    notifier = FakeNotifier()
    agg = DeletionAggregator(config, db, Exporter(db, config), notifier)
    await seed(db, 4, 1, outgoing=True)

    await agg.add(None, [4001])
    await agg.flush()

    assert notifier.texts == [] and notifier.files == []
    assert (await db.get_message(4, 4001))["deleted"] == 1


async def test_media_sent_with_deleted_message(db, config):
    notifier = FakeNotifier()
    agg = DeletionAggregator(config, db, Exporter(db, config), notifier)
    await db.upsert_chat(3, "c", "private", None)
    photo = config.media_dir / "3" / "1_1.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    await db.save_message(MessageRecord(3, 1, "2026-10-01T09:00:00+00:00", sender_id=3, text="смотри",
                                        media_type="photo", media_uid="photo:1", media_path="media/3/1_1.jpg"))

    await agg.add(None, [1])
    await agg.flush()

    assert notifier.files and notifier.files[0][0] == photo
    assert "смотри" in notifier.files[0][1]
