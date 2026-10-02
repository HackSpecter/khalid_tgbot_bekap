import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tgbackup.db import MessageRecord
from tgbackup.evidence import GENESIS_HASH, ChainVerifier, canonical_json, compute_entry_hash, hash_text


async def collect(evidence):
    return [entry async for entry in evidence.iter_all(batch=2)]


async def test_chain_links_and_hashes(evidence):
    first = await evidence.append("new", chat_id=1, message_id=1, raw={"message": "a"}, media_sha256="ab" * 32)
    second = await evidence.append("edit", chat_id=1, message_id=1, raw={"message": "b"})

    assert first.seq == 1 and first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.entry_hash
    assert first.record_hash == hash_text(first.payload)
    assert second.entry_hash == compute_entry_hash(2, first.entry_hash, second.record_hash)
    assert json.loads(first.payload)["media_sha256"] == "ab" * 32


async def test_verifier_accepts_valid_chain(evidence):
    for i in range(5):
        await evidence.append("new", chat_id=1, message_id=i, raw={"i": i})
    verifier = ChainVerifier()
    for entry in await collect(evidence):
        verifier.feed(entry)
    assert verifier.count == 5 and not verifier.issues


async def test_verifier_detects_tampering(evidence):
    for i in range(3):
        await evidence.append("new", chat_id=1, message_id=i, raw={"i": i})
    entries = await collect(evidence)

    forged = list(entries)
    forged[1] = replace(entries[1], payload=entries[1].payload.replace('"i":1', '"i":42'))
    verifier = ChainVerifier()
    for entry in forged:
        verifier.feed(entry)
    assert any("record_hash" in issue.problem for issue in verifier.issues)

    dropped = [entries[0], entries[2]]
    verifier = ChainVerifier()
    for entry in dropped:
        verifier.feed(entry)
    problems = " ".join(issue.problem for issue in verifier.issues)
    assert "разрыв нумерации" in problems and "prev_hash" in problems


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evidence_log SET payload = 'x' WHERE seq = 1",
        "DELETE FROM evidence_log WHERE seq = 1",
    ],
)
async def test_evidence_log_is_append_only(db, evidence, sql):
    await evidence.append("new", chat_id=1, message_id=1)
    with pytest.raises(Exception, match="append-only"):
        await db.conn.execute(sql)


async def test_edits_and_profiles_are_append_only(db):
    old_record = MessageRecord(1, 1, "2026-10-01T00:00:00+00:00", text="a")
    await db.save_message(old_record)
    await db.record_edit(await db.get_message(1, 1), MessageRecord(1, 1, old_record.date, text="b"))
    with pytest.raises(Exception, match="append-only"):
        await db.conn.execute("UPDATE edits SET old_text = 'x'")
    with pytest.raises(Exception, match="append-only"):
        await db.conn.execute("DELETE FROM edits")

    await db.upsert_user(5, "A", None, None, None, False)
    await db.add_profile_snapshot({"user_id": 5, "observed_at": "2026-10-01", "source": "event", "changes": "{}"})
    with pytest.raises(Exception, match="append-only"):
        await db.conn.execute("UPDATE profile_history SET first_name = 'x'")


async def test_messages_never_deleted_and_identity_immutable(db, evidence):
    await db.save_message(MessageRecord(1, 1, "2026-10-01T00:00:00+00:00", text="a"))
    entry = await evidence.append("new", chat_id=1, message_id=1)
    await db.link_evidence(1, 1, entry.seq, entry.record_hash)

    with pytest.raises(Exception, match="never deleted"):
        await db.conn.execute("DELETE FROM messages")
    with pytest.raises(Exception, match="immutable"):
        await db.conn.execute("UPDATE messages SET date = '2020-01-01' WHERE message_id = 1")
    await db.conn.execute("UPDATE messages SET text = 'b' WHERE message_id = 1")


def test_canonical_json_serializes_telethon_types():
    value = {"b": b"\x00\x01", "d": datetime(2026, 1, 1, tzinfo=timezone.utc), "a": 1}
    assert canonical_json(value) == '{"a":1,"b":"base64:AAE=","d":"2026-01-01T00:00:00+00:00"}'
