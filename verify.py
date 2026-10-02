import argparse
import asyncio
import json
import sys

from tgbackup.anchoring import (
    Anchorer,
    inspect_ots,
    openssl_ts_verify,
    parse_anchor_text,
    parse_tsr,
    verify_ots_with_explorer,
)
from tgbackup.config import ConfigError, load_config
from tgbackup.crypto import sha256_hex
from tgbackup.evidence import ChainVerifier
from tgbackup.main import open_storage


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def section(self, title: str) -> None:
        print(f"\n== {title}")

    def ok(self, text: str) -> None:
        print(f"  [OK]   {text}")

    def fail(self, text: str) -> None:
        self.failures += 1
        print(f"  [FAIL] {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(f"  [WARN] {text}")


async def check_chain(
    storage, report: Report, limit_issues: int, anchor_seqs: set[int]
) -> tuple[dict[int, str], list[tuple[int, str, str]]]:
    report.section("Хеш-цепочка evidence_log")
    verifier = ChainVerifier()
    heads: dict[int, str] = {}
    media: list[tuple[int, str, str]] = []
    async for entry in storage.evidence.iter_all():
        verifier.feed(entry)
        if entry.seq in anchor_seqs:
            heads[entry.seq] = entry.entry_hash
        if entry.media_sha256 and entry.media_path:
            media.append((entry.seq, entry.media_path, entry.media_sha256))
    for issue in verifier.issues[:limit_issues]:
        report.fail(f"seq {issue.seq}: {issue.problem}")
    if len(verifier.issues) > limit_issues:
        report.fail(f"… и ещё {len(verifier.issues) - limit_issues} нарушений")
    if not verifier.issues:
        last = verifier.last
        report.ok(
            f"{verifier.count} записей, цепочка непрерывна"
            + (f", голова seq {last.seq} {last.entry_hash}" if last else "")
        )
    return heads, media


async def check_append_only(storage, report: Report) -> None:
    report.section("Защита append-only")
    async with storage.db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ) as cur:
        triggers = {row[0] for row in await cur.fetchall()}
    expected = {
        "evidence_log_no_update", "evidence_log_no_delete", "edits_no_update", "edits_no_delete",
        "profile_history_no_update", "profile_history_no_delete", "messages_no_delete",
        "messages_immutable_original",
    }
    missing = expected - triggers
    if missing:
        report.fail(f"отсутствуют триггеры: {', '.join(sorted(missing))}")
    else:
        report.ok("все триггеры append-only на месте")


async def check_media(storage, refs: list[tuple[int, str, str]], report: Report, skip: bool) -> None:
    report.section("Медиафайлы (SHA-256 на момент получения)")
    if skip:
        report.warn(f"пропущено ({len(refs)} файлов), запустите без --skip-media")
        return
    missing = mismatched = 0
    for seq, rel, expected in refs:
        if not storage.vault.exists(rel):
            missing += 1
            report.fail(f"seq {seq}: файл отсутствует {rel}")
            continue
        try:
            actual = sha256_hex(await asyncio.to_thread(storage.vault.read, rel))
        except Exception as exc:
            mismatched += 1
            report.fail(f"seq {seq}: не удаётся расшифровать {rel}: {exc}")
            continue
        if actual != expected:
            mismatched += 1
            report.fail(f"seq {seq}: хеш файла изменился {rel}")
    if not missing and not mismatched:
        report.ok(f"{len(refs)} файлов совпадают с хешами из цепочки")


async def check_projection(storage, report: Report) -> None:
    report.section("Согласованность индекса messages с цепочкой")
    db = storage.db
    unlinked = await db.scalar("SELECT COUNT(*) FROM messages WHERE first_seq IS NULL")
    if unlinked:
        report.warn(f"{unlinked} сообщений без записи в цепочке (будут дописаны при следующем старте)")
    bad = await db.scalar(
        """
        SELECT COUNT(*) FROM messages m
        LEFT JOIN evidence_log e ON e.seq = m.first_seq
        WHERE m.first_seq IS NOT NULL AND (
            e.seq IS NULL OR e.record_hash != m.record_hash OR e.chat_id != m.chat_id OR e.message_id != m.message_id
        )
        """
    )
    if bad:
        report.fail(f"{bad} сообщений ссылаются на несоответствующие записи цепочки")
    deleted_without_event = await db.scalar(
        """
        SELECT COUNT(*) FROM messages m WHERE m.deleted = 1 AND NOT EXISTS (
            SELECT 1 FROM evidence_log e WHERE e.event = 'delete' AND e.chat_id = m.chat_id
            AND EXISTS (SELECT 1 FROM json_each(json_extract(e.payload, '$.data.message_ids')) j WHERE j.value = m.message_id)
        )
        """
    )
    if deleted_without_event:
        report.warn(f"{deleted_without_event} удалённых сообщений без события delete (удалены до версии 2)")
    backfilled = await db.scalar(
        "SELECT COUNT(*) FROM evidence_log WHERE event = 'new' AND json_extract(payload, '$.data.backfilled') = 1"
    )
    if backfilled:
        report.warn(f"{backfilled} записей перенесены из версии 1 без raw-объекта (помечены backfilled)")
    if not bad:
        report.ok("каждое сообщение указывает на свою исходную запись")


async def check_anchors(storage, config, heads: dict[int, str], report: Report, online: bool) -> None:
    report.section("Якоря и внешние метки времени")
    anchorer = Anchorer(config, storage.db, storage.evidence)
    anchors = await storage.db.anchors()
    if not anchors:
        report.warn("якорей ещё нет (первый создаётся ежедневно в ANCHOR_HOUR или командой /anchor)")
        return
    for anchor in anchors:
        label = f"якорь #{anchor['id']} seq {anchor['seq']}"
        path = config.data_dir / anchor["anchor_file"]
        if not path.exists():
            report.fail(f"{label}: файл якоря отсутствует")
            continue
        content = path.read_bytes()
        if sha256_hex(content) != anchor["anchor_sha256"]:
            report.fail(f"{label}: файл якоря изменён")
            continue
        values = parse_anchor_text(content.decode())
        if heads.get(int(values.get("seq", 0))) != values.get("entry_hash"):
            report.fail(f"{label}: не совпадает с цепочкой")
            continue
        report.ok(f"{label}: соответствует цепочке ({anchor['created_at']})")

        if anchor["tsr_file"]:
            tsr = config.data_dir / anchor["tsr_file"]
            parsed = parse_tsr(tsr.read_bytes(), bytes.fromhex(anchor["anchor_sha256"]))
            if not parsed.ok:
                report.fail(f"    RFC 3161: {parsed.error}")
            else:
                verified, output = openssl_ts_verify(path, tsr, anchorer.ca_file, anchorer.tsa_cert)
                if verified:
                    report.ok(f"    RFC 3161: подпись {parsed.tsa} верна, время {parsed.gen_time}")
                elif verified is None:
                    report.warn(f"    RFC 3161: imprint совпадает, время {parsed.gen_time}; {output}")
                else:
                    report.fail(f"    RFC 3161: openssl ts -verify не прошёл: {output}")
        else:
            report.warn(f"    RFC 3161: метки нет ({anchor['tsa_error'] or 'выключено'})")

        if anchor["ots_file"]:
            ots = config.data_dir / anchor["ots_file"]
            info = await asyncio.to_thread(inspect_ots, ots, bytes.fromhex(anchor["anchor_sha256"]))
            if info.status == "complete":
                if online:
                    checked = await asyncio.to_thread(verify_ots_with_explorer, ots)
                    if checked.verified:
                        report.ok(f"    OpenTimestamps: Bitcoin блок {min(info.blocks)}, время блока {checked.block_time}")
                    elif checked.verified is False:
                        report.fail(f"    OpenTimestamps: {checked.error}")
                    else:
                        report.warn(f"    OpenTimestamps: блок {min(info.blocks)}, {checked.error}")
                else:
                    report.ok(f"    OpenTimestamps: подтверждено в блоке {min(info.blocks)} (без онлайн-сверки)")
            elif info.status == "pending":
                report.warn("    OpenTimestamps: ожидает подтверждения в Bitcoin (обновляется автоматически)")
            else:
                report.fail(f"    OpenTimestamps: {info.error}")
        else:
            report.warn(f"    OpenTimestamps: нет ({anchor['ots_error'] or 'выключено'})")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка целостности архива переписок")
    parser.add_argument("--skip-media", action="store_true", help="не пересчитывать хеши медиафайлов")
    parser.add_argument("--offline", action="store_true", help="не сверять OpenTimestamps с блокчейном")
    parser.add_argument("--max-issues", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="вывести итог в JSON последней строкой")
    args = parser.parse_args()

    config = load_config()
    storage = await open_storage(config, maintenance=False)
    report = Report()
    try:
        stats = await storage.db.stats()
        print(f"Архив: {config.db_path} ({'SQLCipher' if stats.encrypted else 'без шифрования'}), "
              f"сообщений {stats.messages}, записей цепочки {stats.evidence}")
        anchor_seqs = {anchor["seq"] for anchor in await storage.db.anchors()}
        heads, media = await check_chain(storage, report, args.max_issues, anchor_seqs)
        await check_append_only(storage, report)
        await check_media(storage, media, report, args.skip_media)
        await check_projection(storage, report)
        await check_anchors(storage, config, heads, report, online=not args.offline)
    finally:
        await storage.close()

    print()
    if report.failures:
        print(f"ИТОГ: НАРУШЕНИЯ ЦЕЛОСТНОСТИ — {report.failures} ошибок, {report.warnings} предупреждений")
    else:
        print(f"ИТОГ: целостность подтверждена ({report.warnings} предупреждений)")
    if args.json:
        print(json.dumps({"failures": report.failures, "warnings": report.warnings}))
    return 1 if report.failures else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")
