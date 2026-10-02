import zipfile
from dataclasses import replace

from tgbackup.db import MessageRecord
from tgbackup.exporter import Exporter

CHAT = 555


async def seed(db, config):
    await db.upsert_chat(CHAT, "Иван <script>", "private", "ivan")
    photo = config.media_dir / str(CHAT) / "2_99.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"\xff\xd8" + b"0" * 1000)

    await db.save_message(MessageRecord(CHAT, 1, "2026-09-30T08:00:00+00:00", sender_id=CHAT, sender_name="Иван", text="Привет <b>"))
    await db.save_message(
        MessageRecord(
            CHAT, 2, "2026-10-01T09:00:00+00:00", sender_id=1, outgoing=True, text="Фото", reply_to=1,
            media_type="photo", media_uid="photo:99", media_path=f"media/{CHAT}/2_99.jpg",
        )
    )
    old = await db.get_message(CHAT, 1)
    await db.record_edit(old, MessageRecord(CHAT, 1, old["date"], text="Привет!", edit_date="2026-09-30T08:05:00+00:00"))
    await db.save_message(
        MessageRecord(CHAT, 1, "2026-09-30T08:00:00+00:00", sender_id=CHAT, sender_name="Иван", text="Привет!",
                      edited=True, edit_date="2026-09-30T08:05:00+00:00")
    )
    await db.save_message(MessageRecord(CHAT, 3, "2026-10-01T09:30:00+00:00", sender_id=CHAT, sender_name="Иван", text="секрет"))
    rows = await db.find_for_deletion([3], None)
    await db.mark_deleted([r["id"] for r in rows], "2026-10-01T10:00:00+00:00")


async def test_export_single_zip(db, config):
    await seed(db, config)
    parts = await Exporter(db, config).export(CHAT)
    assert len(parts) == 1

    with zipfile.ZipFile(parts[0]) as archive:
        names = archive.namelist()
        html = archive.read("chat.html").decode()
    assert "media/2_99.jpg" in names
    assert '<img src="media/2_99.jpg"' in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Привет &lt;b&gt;" in html
    assert "секрет" in html and "удалено" in html
    assert "изменено" in html and "История изменений (1)" in html
    assert 'href="#m1"' in html
    assert "30 сентября 2026" in html and "1 октября 2026" in html
    assert 'class="row out' in html


async def test_export_splits_into_parts(db, config):
    small = replace(config, export_part_size_mb=1)
    await db.upsert_chat(CHAT, "Big", "private", None)
    folder = config.media_dir / str(CHAT)
    folder.mkdir(parents=True)
    for i in range(1, 4):
        path = folder / f"{i}_{i}.bin"
        path.write_bytes(b"x" * 600_000)
        await db.save_message(
            MessageRecord(CHAT, i, f"2026-10-01T09:0{i}:00+00:00", text=f"m{i}",
                          media_type="document", media_uid=f"doc:{i}", media_path=f"media/{CHAT}/{i}_{i}.bin")
        )

    parts = await Exporter(db, small).export(CHAT)
    assert len(parts) == 3
    assert all(p.name.endswith(f"_part{i}.zip") for i, p in enumerate(parts, 1))
    for i, part in enumerate(parts, 1):
        with zipfile.ZipFile(part) as archive:
            assert archive.namelist() == ["chat.html", f"media/{i}_{i}.bin"]
            assert f"часть {i} из 3" in archive.read("chat.html").decode()


async def test_oversized_media_left_out(db, config):
    small = replace(config, export_part_size_mb=1)
    await db.upsert_chat(CHAT, "Big", "private", None)
    path = config.media_dir / str(CHAT) / "1_1.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 2_000_000)
    await db.save_message(
        MessageRecord(CHAT, 1, "2026-10-01T09:00:00+00:00", media_type="video", media_uid="doc:1",
                      media_path=f"media/{CHAT}/1_1.mp4")
    )
    parts = await Exporter(db, small).export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        assert archive.namelist() == ["chat.html"]
        assert "не поместился в архив" in archive.read("chat.html").decode()


async def test_export_empty_chat(db, config):
    await db.upsert_chat(CHAT, "Empty", "private", None)
    parts = await Exporter(db, config).export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        assert "Сохранённых сообщений нет" in archive.read("chat.html").decode()
