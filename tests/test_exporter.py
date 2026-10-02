import json
import subprocess
import sys
import zipfile
from dataclasses import replace

import pytest

from tgbackup.crypto import verify_signature
from tgbackup.db import MessageRecord
from tgbackup.exporter import Exporter

CHAT = 555


async def add(db, evidence, record, raw=None, media_sha=None):
    await db.save_message(record)
    entry = await evidence.append(
        "new", chat_id=record.chat_id, message_id=record.message_id, raw=raw or {"message": record.text},
        media_sha256=media_sha, media_path=record.media_path if media_sha else None,
    )
    await db.link_evidence(record.chat_id, record.message_id, entry.seq, entry.record_hash)
    if media_sha:
        await db.set_media(record.chat_id, record.message_id, record.media_path, record.media_size, None, media_sha)
    return entry


async def seed(db, evidence, vault):
    await db.upsert_chat(CHAT, "Иван <script>", "private", "ivan")
    await db.upsert_user(CHAT, "Иван", "Петров", "ivan", "992900000000", False)
    rel, digest, size = vault.write(f"media/{CHAT}/2_99.jpg", b"\xff\xd8" + b"0" * 1000)

    await add(db, evidence, MessageRecord(CHAT, 1, "2026-09-30T08:00:00+00:00", sender_id=CHAT, sender_name="Иван", text="Привет <b>"))
    await add(
        db, evidence,
        MessageRecord(CHAT, 2, "2026-10-01T09:00:00+00:00", sender_id=1, outgoing=True, text="Фото", reply_to=1,
                      media_type="photo", media_uid="photo:99", media_path=rel, media_size=size,
                      fwd_from_id=777, fwd_from_name="Канал новостей"),
        media_sha=digest,
    )
    old = await db.get_message(CHAT, 1)
    new = MessageRecord(CHAT, 1, "2026-09-30T08:00:00+00:00", sender_id=CHAT, sender_name="Иван", text="Привет!",
                        edited=True, edit_date="2026-09-30T08:05:00+00:00")
    entry = await evidence.append("edit", chat_id=CHAT, message_id=1, raw={"message": "Привет!"})
    await db.record_edit(old, new, entry.seq)
    await db.save_message(new)
    await add(db, evidence, MessageRecord(CHAT, 3, "2026-10-01T09:30:00+00:00", sender_id=CHAT, sender_name="Иван", text="секрет"))
    await add(db, evidence, MessageRecord(CHAT, 4, "2026-10-01T09:40:00+00:00", sender_id=CHAT, outgoing=False,
                                          media_type="service", service_action="📞 Звонок (входящий), пропущен"))
    rows = await db.find_for_deletion([3], None)
    await db.mark_deleted([r["id"] for r in rows], "2026-10-01T10:00:00+00:00")
    await evidence.append("delete", chat_id=CHAT, data={"message_ids": [3]})
    await evidence.append("new", chat_id=999, message_id=1, raw={"message": "чужой чат"})
    return digest


def run_verifier(folder):
    return subprocess.run([sys.executable, str(folder / "verify_export.py"), str(folder)], capture_output=True, text=True)


async def test_export_contents(db, config, exporter, evidence, vault, tmp_path):
    digest = await seed(db, evidence, vault)
    parts = await exporter.export(CHAT)
    assert len(parts) == 1

    with zipfile.ZipFile(parts[0]) as archive:
        names = set(archive.namelist())
        html = archive.read("chat.html").decode()
        manifest_bytes = archive.read("manifest.json")
        signature = archive.read("manifest.sig")
        public = archive.read("public_key.pem")
        records = [json.loads(line) for line in archive.read("evidence/records.jsonl").decode().splitlines()]
        readme = archive.read("README.txt").decode()

    assert {"chat.html", "manifest.json", "manifest.sig", "public_key.pem", "README.txt", "verify_export.py",
            "evidence/records.jsonl", "evidence/chain.csv", "media/2_99.jpg"} <= names
    assert '<img src="media/2_99.jpg"' in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Привет &lt;b&gt;" in html
    assert "секрет" in html and "удалено" in html
    assert "История изменений (1)" in html
    assert "#2 · 2026-10-01 09:00:00 UTC" in html
    assert "Переслано от Канал новостей" in html
    assert "📞 Звонок (входящий), пропущен" in html
    assert "992900000000" in html and "Иван Петров" in html

    assert verify_signature(public, manifest_bytes, signature)
    manifest = json.loads(manifest_bytes)
    assert manifest["chat"]["id"] == CHAT
    assert manifest["interlocutor"]["user_id"] == CHAT
    assert manifest["files"]["media/2_99.jpg"] == digest
    assert {m["message_id"] for m in manifest["messages"]} == {1, 2, 3, 4}
    assert all("чужой чат" not in r["payload"] for r in records)
    assert {r["event"] for r in records} == {"new", "edit", "delete"}
    assert "verify_export.py" in readme


async def test_standalone_verifier_detects_tampering(db, config, exporter, evidence, vault, tmp_path):
    await seed(db, evidence, vault)
    parts = await exporter.export(CHAT)
    folder = tmp_path / "unzipped"
    with zipfile.ZipFile(parts[0]) as archive:
        archive.extractall(folder)

    result = run_verifier(folder)
    assert result.returncode == 0, result.stdout
    assert "целостность подтверждена" in result.stdout

    (folder / "media" / "2_99.jpg").write_bytes(b"forged")
    result = run_verifier(folder)
    assert result.returncode == 1
    assert "хеш не совпадает: media/2_99.jpg" in result.stdout
    assert "медиафайл media/2_99.jpg не совпадает" in result.stdout


async def test_standalone_verifier_detects_record_edit(db, config, exporter, evidence, vault, tmp_path):
    await seed(db, evidence, vault)
    parts = await exporter.export(CHAT)
    folder = tmp_path / "unzipped"
    with zipfile.ZipFile(parts[0]) as archive:
        archive.extractall(folder)
    path = folder / "evidence" / "records.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("секрет", "подделка"), encoding="utf-8")

    result = run_verifier(folder)
    assert result.returncode == 1
    assert "содержимое записи не совпадает" in result.stdout


async def test_export_splits_into_parts(db, config, vault, evidence, signer):
    small = replace(config, export_part_size_mb=1)
    exporter = Exporter(db, small, vault, evidence, signer)
    await db.upsert_chat(CHAT, "Big", "private", None)
    for i in range(1, 4):
        rel, digest, size = vault.write(f"media/{CHAT}/{i}_{i}.bin", b"x" * 600_000)
        await add(db, evidence, MessageRecord(CHAT, i, f"2026-10-01T09:0{i}:00+00:00", text=f"m{i}",
                                              media_type="document", media_uid=f"doc:{i}", media_path=rel,
                                              media_size=size), media_sha=digest)

    parts = await exporter.export(CHAT)
    assert len(parts) == 3
    for i, part in enumerate(parts, 1):
        assert part.name.endswith(f"_part{i}.zip")
        with zipfile.ZipFile(part) as archive:
            media = [n for n in archive.namelist() if n.startswith("media/")]
            assert media == [f"media/{i}_{i}.bin"]
            assert f"часть {i} из 3" in archive.read("chat.html").decode()
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["part"] == i and manifest["parts"] == 3


async def test_oversized_media_left_out(db, config, vault, evidence, signer):
    small = replace(config, export_part_size_mb=1)
    exporter = Exporter(db, small, vault, evidence, signer)
    await db.upsert_chat(CHAT, "Big", "private", None)
    rel, digest, size = vault.write(f"media/{CHAT}/1_1.mp4", b"x" * 2_000_000)
    await db.save_message(MessageRecord(CHAT, 1, "2026-10-01T09:00:00+00:00", media_type="video", media_uid="doc:1",
                                        media_path=rel, media_size=size))
    parts = await exporter.export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        assert not [n for n in archive.namelist() if n.startswith("media/")]
        assert "не поместился в архив" in archive.read("chat.html").decode()


async def test_export_empty_chat(db, exporter):
    await db.upsert_chat(CHAT, "Empty", "private", None)
    parts = await exporter.export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        assert "Сохранённых сообщений нет" in archive.read("chat.html").decode()


async def test_pdf_export(db, config, vault, evidence, signer):
    pytest.importorskip("weasyprint")
    exporter = Exporter(db, replace(config, export_pdf=True), vault, evidence, signer)
    await seed(db, evidence, vault)
    parts = await exporter.export(CHAT)
    with zipfile.ZipFile(parts[0]) as archive:
        pdf = archive.read("chat.pdf")
        manifest = json.loads(archive.read("manifest.json"))
    assert pdf.startswith(b"%PDF")
    assert "chat.pdf" in manifest["files"]
