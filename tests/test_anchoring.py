import hashlib
import json
import zipfile
from pathlib import Path

from tgbackup.anchoring import Anchorer, anchor_text, build_tsq, parse_anchor_text, parse_tsr
from tgbackup.db import MessageRecord

DATA = Path(__file__).parent / "data"


def test_build_tsq_contains_digest():
    from asn1crypto import tsp

    digest = hashlib.sha256(b"x").digest()
    der, nonce = build_tsq(digest)
    request = tsp.TimeStampReq.load(der)
    assert request["message_imprint"]["hashed_message"].native == digest
    assert request["nonce"].native == nonce


def test_parse_real_freetsa_response():
    data = (DATA / "freetsa_sha256_test.tsr").read_bytes()
    result = parse_tsr(data, hashlib.sha256(b"test").digest())
    assert result.ok and result.gen_time.startswith("2026-10-02")
    assert "freetsa" in result.tsa.lower()
    assert not parse_tsr(data, hashlib.sha256(b"other").digest()).ok


def test_anchor_text_roundtrip():
    values = parse_anchor_text(anchor_text(7, "ab" * 32, "2026-10-02T00:00:00Z"))
    assert values["seq"] == "7" and values["entry_hash"] == "ab" * 32


async def test_anchor_now_records_and_chains(db, config, evidence):
    anchorer = Anchorer(config, db, evidence)
    assert await anchorer.anchor_now() is None

    await evidence.append("new", chat_id=1, message_id=1)
    head = await evidence.head()
    result = await anchorer.anchor_now()
    assert result["seq"] == head[0] and result["entry_hash"] == head[1]
    anchor_file = config.data_dir / result["anchor_file"]
    assert hashlib.sha256(anchor_file.read_bytes()).hexdigest() == result["anchor_sha256"]

    entry = await evidence.entry_at(head[0] + 1)
    assert entry.event == "anchor"
    assert json.loads(entry.payload)["data"]["anchored_entry_hash"] == head[1]
    assert await anchorer.anchor_now() is None


async def test_export_includes_anchor(db, config, evidence, exporter, tmp_path):
    import subprocess
    import sys

    await db.upsert_chat(5, "A", "private", None)
    await db.save_message(MessageRecord(5, 1, "2026-10-01T00:00:00+00:00", text="hi"))
    entry = await evidence.append("new", chat_id=5, message_id=1, raw={"m": "hi"})
    await db.link_evidence(5, 1, entry.seq, entry.record_hash)
    await Anchorer(config, db, evidence).anchor_now()

    parts = await exporter.export(5)
    folder = tmp_path / "x"
    with zipfile.ZipFile(parts[0]) as archive:
        archive.extractall(folder)
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["anchors"]) == 1
    assert (folder / manifest["anchors"][0]["anchor_file"]).exists()
    result = subprocess.run([sys.executable, str(folder / "verify_export.py"), str(folder)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    assert "якорь на seq 1" in result.stdout
