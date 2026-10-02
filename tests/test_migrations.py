import json
import sqlite3

import pytest

from tgbackup.crypto import KeyRing, Vault
from tgbackup.db import Database, is_plaintext_db
from tgbackup.evidence import ChainVerifier, EvidenceLog
from tgbackup.maintenance import run_maintenance
from tgbackup.migrations import LATEST, V1_BASE

SECRET = "b" * 64


def make_v1(path, media_dir):
    conn = sqlite3.connect(path)
    conn.executescript(V1_BASE)
    conn.execute("INSERT INTO chats VALUES (42, 'Ali', 'private', 'ali', '2026-01-01')")
    conn.execute(
        "INSERT INTO messages (chat_id, message_id, sender_id, text, date, media_type, media_path, deleted, saved_at)"
        " VALUES (42, 1, 42, 'old message', '2026-01-01T00:00:00+00:00', 'photo', 'media/42/1_5.jpg', 0, '2026')"
    )
    conn.execute(
        "INSERT INTO messages (chat_id, message_id, sender_id, text, date, deleted, deleted_at, saved_at)"
        " VALUES (42, 2, 42, 'gone', '2026-01-01T00:01:00+00:00', 1, '2026-01-02', '2026')"
    )
    conn.commit()
    conn.close()
    (media_dir / "42").mkdir(parents=True)
    (media_dir / "42" / "1_5.jpg").write_bytes(b"legacy-photo")


async def test_v1_database_migrates_and_backfills(config):
    make_v1(config.db_path, config.media_dir)
    db = Database(config.db_path)
    await db.connect()
    try:
        assert await db.scalar("SELECT MAX(version) FROM schema_migrations") == LATEST
        evidence = EvidenceLog(db)
        await run_maintenance(db, evidence, Vault(config.data_dir, KeyRing(None)))

        rows = await db.chat_messages(42)
        assert all(row["first_seq"] for row in rows)
        verifier = ChainVerifier()
        async for entry in evidence.iter_all():
            verifier.feed(entry)
            payload = json.loads(entry.payload)
            assert payload["data"]["backfilled"] is True
            assert payload["raw"] is None
        assert verifier.count == 2 and not verifier.issues
        assert rows[0]["text"] == "old message" and rows[1]["deleted"] == 1

        await run_maintenance(db, evidence, Vault(config.data_dir, KeyRing(None)))
        assert await db.scalar("SELECT COUNT(*) FROM evidence_log") == 2
    finally:
        await db.close()


async def test_v1_plaintext_converted_to_sqlcipher_and_media_encrypted(config):
    pytest.importorskip("sqlcipher3")
    make_v1(config.db_path, config.media_dir)
    ring = KeyRing.from_secret(SECRET)

    db = Database(config.db_path, ring.db_key_hex)
    await db.connect()
    vault = Vault(config.data_dir, ring)
    await run_maintenance(db, EvidenceLog(db), vault)
    row = await db.get_message(42, 1)
    await db.close()

    assert not is_plaintext_db(config.db_path)
    assert b"old message" not in config.db_path.read_bytes()
    assert row["media_path"].endswith(".enc")
    assert vault.read(row["media_path"]) == b"legacy-photo"
    assert not (config.media_dir / "42" / "1_5.jpg").exists()

    with pytest.raises(Exception):
        wrong = Database(config.db_path, KeyRing.from_secret("c" * 64).db_key_hex)
        await wrong.connect()

    again = Database(config.db_path, ring.db_key_hex)
    await again.connect()
    assert (await again.get_message(42, 1))["text"] == "old message"
    await again.close()
