#!/usr/bin/env python3
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

GENESIS = "0" * 64


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_hash(seq, prev_hash, record_hash):
    return hashlib.sha256(f"{seq}:{prev_hash}:{record_hash}".encode()).hexdigest()


class Report:
    def __init__(self):
        self.failures = 0
        self.warnings = 0

    def ok(self, text):
        print(f"  [OK]   {text}")

    def fail(self, text):
        self.failures += 1
        print(f"  [FAIL] {text}")

    def warn(self, text):
        self.warnings += 1
        print(f"  [WARN] {text}")


def check_files(root, manifest, report):
    print("1. Целостность файлов (SHA-256 из manifest.json)")
    bad = 0
    for name, expected in sorted(manifest["files"].items()):
        path = root / name
        if not path.is_file():
            report.fail(f"файл отсутствует: {name}")
            bad += 1
        elif sha256_file(path) != expected:
            report.fail(f"хеш не совпадает: {name}")
            bad += 1
    if not bad:
        report.ok(f"все {len(manifest['files'])} файлов совпадают с манифестом")


def check_signature(root, manifest, report):
    print("2. Подпись манифеста (Ed25519)")
    data = (root / "manifest.json").read_bytes()
    signature = (root / "manifest.sig").read_bytes()
    public_pem = (root / "public_key.pem").read_bytes()
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        openssl = shutil.which("openssl")
        if not openssl:
            report.warn("нет ни пакета cryptography, ни openssl — подпись не проверена")
            return
        result = subprocess.run(
            [openssl, "pkeyutl", "-verify", "-pubin", "-inkey", str(root / "public_key.pem"), "-rawin",
             "-in", str(root / "manifest.json"), "-sigfile", str(root / "manifest.sig")],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            report.ok("подпись верна (openssl)")
        else:
            report.fail("подпись НЕ верна (openssl): " + (result.stdout + result.stderr).strip())
        return
    key = serialization.load_pem_public_key(public_pem)
    try:
        key.verify(signature, data)
        report.ok("подпись верна")
    except Exception:
        report.fail("подпись НЕ верна — манифест изменён после подписания")
        return
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = hashlib.sha256(raw).hexdigest()
    expected = manifest.get("signature", {}).get("public_key_sha256")
    if expected and expected != fingerprint:
        report.fail("отпечаток публичного ключа не совпадает с манифестом")
    else:
        report.ok(f"отпечаток ключа: {fingerprint}")


def load_chain(root):
    rows = []
    with open(root / "evidence" / "chain.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            row["seq"] = int(row["seq"])
            rows.append(row)
    return rows


def check_chain(root, manifest, report):
    print("3. Хеш-цепочка")
    chain = load_chain(root)
    if not chain:
        report.warn("цепочка пустая")
        return {}
    by_seq = {}
    previous = None
    broken = 0
    for row in chain:
        if previous is not None:
            if row["seq"] != previous["seq"] + 1:
                report.fail(f"разрыв нумерации перед seq {row['seq']}")
                broken += 1
            if row["prev_hash"] != previous["entry_hash"]:
                report.fail(f"seq {row['seq']}: prev_hash не совпадает с предыдущей записью")
                broken += 1
        elif row["seq"] == 1 and row["prev_hash"] != GENESIS:
            report.fail("первая запись цепочки ссылается не на genesis")
            broken += 1
        if entry_hash(row["seq"], row["prev_hash"], row["record_hash"]) != row["entry_hash"]:
            report.fail(f"seq {row['seq']}: entry_hash вычислен неверно")
            broken += 1
        by_seq[row["seq"]] = row
        previous = row
    chain_info = manifest.get("chain", {})
    if chain[-1]["entry_hash"] != chain_info.get("head_hash"):
        report.fail("голова цепочки не совпадает с манифестом")
        broken += 1
    if not broken:
        report.ok(f"цепочка непрерывна: seq {chain[0]['seq']}…{chain[-1]['seq']}, голова {chain[-1]['entry_hash'][:16]}…")
    return by_seq


def check_records(root, manifest, chain, report):
    print("4. Записи о сообщениях этого чата")
    bad = 0
    count = 0
    hashes = []
    with open(root / "evidence" / "records.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            count += 1
            seq = record["seq"]
            if hashlib.sha256(record["payload"].encode("utf-8")).hexdigest() != record["record_hash"]:
                report.fail(f"seq {seq}: содержимое записи не совпадает с record_hash")
                bad += 1
            link = chain.get(seq)
            if link is None:
                report.fail(f"seq {seq}: запись отсутствует в цепочке")
                bad += 1
            elif link["record_hash"] != record["record_hash"] or link["entry_hash"] != record["entry_hash"]:
                report.fail(f"seq {seq}: запись не совпадает с цепочкой")
                bad += 1
            payload = json.loads(record["payload"])
            media_sha = payload.get("media_sha256")
            media_file = record.get("media_file")
            if media_sha and media_file:
                path = root / media_file
                if path.is_file() and sha256_file(path) != media_sha:
                    report.fail(f"seq {seq}: медиафайл {media_file} не совпадает с хешем на момент получения")
                    bad += 1
            hashes.append(record["entry_hash"])
    digest = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    if digest != manifest.get("records_digest"):
        report.fail("records_digest не совпадает с манифестом")
        bad += 1
    if not bad:
        report.ok(f"{count} записей подтверждены, records_digest {digest[:16]}…")


def check_anchors(root, manifest, chain, report):
    print("5. Внешние метки времени")
    anchors = manifest.get("anchors", [])
    if not anchors:
        report.warn("в экспорте нет якорей (цепочка ещё не заякорена после этих записей)")
        return
    openssl = shutil.which("openssl")
    ca = root / "evidence" / "anchors" / "freetsa_cacert.pem"
    tsa = root / "evidence" / "anchors" / "freetsa_tsa.crt"
    for anchor in anchors:
        name = anchor["anchor_file"]
        path = root / name
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        values = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
        seq = int(values.get("seq", 0))
        link = chain.get(seq)
        if link is None or link["entry_hash"] != values.get("entry_hash"):
            report.fail(f"{name}: якорь не соответствует цепочке (seq {seq})")
            continue
        report.ok(f"{name}: якорь на seq {seq}")
        if anchor.get("tsr_file"):
            if openssl and ca.exists() and tsa.exists():
                result = subprocess.run(
                    [openssl, "ts", "-verify", "-data", str(path), "-in", str(root / anchor["tsr_file"]),
                     "-CAfile", str(ca), "-untrusted", str(tsa)],
                    capture_output=True, text=True,
                )
                output = (result.stdout + result.stderr).strip()
                if result.returncode == 0 and "Verification: OK" in output:
                    report.ok(f"    RFC 3161 (FreeTSA): подпись TSA верна, время {anchor.get('tsa_time')}")
                else:
                    report.fail(f"    RFC 3161: проверка не прошла: {output}")
            else:
                report.warn(f"    RFC 3161: нет openssl — проверьте командой из README.txt (время {anchor.get('tsa_time')})")
        if anchor.get("ots_file"):
            status = anchor.get("ots_status")
            block = anchor.get("ots_block")
            if status == "complete":
                report.ok(f"    OpenTimestamps: подтверждено в Bitcoin, блок {block}; проверка: ots verify {anchor['ots_file']}")
            else:
                report.warn(f"    OpenTimestamps: статус {status}; обновите: ots upgrade {anchor['ots_file']}")


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    print(f"Проверка экспорта: {manifest['chat'].get('title')} (id {manifest['chat'].get('id')}), "
          f"часть {manifest.get('part')}/{manifest.get('parts')}, создан {manifest.get('exported_at_utc')}")
    report = Report()
    check_files(root, manifest, report)
    check_signature(root, manifest, report)
    chain = check_chain(root, manifest, report)
    check_records(root, manifest, chain, report)
    check_anchors(root, manifest, chain, report)
    print()
    if report.failures:
        print(f"ИТОГ: НАРУШЕНИЯ ЦЕЛОСТНОСТИ — {report.failures} ошибок, {report.warnings} предупреждений")
        return 1
    print(f"ИТОГ: целостность подтверждена ({report.warnings} предупреждений)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
