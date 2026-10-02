from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from . import __version__
from .config import Config
from .crypto import KeyRing, Signer, Vault, sha256_hex
from .db import Database, from_iso
from .evidence import EVENT_DELETE, EVENT_PROFILE, EVENT_READ, Entry, EvidenceLog, canonical_json
from .extract import reactions_label
from .formatting import fmt_dt, full_name, human_size, media_label, sender_label

log = logging.getLogger(__name__)

PALETTE = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
STORED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".oga", ".ogg", ".mp3", ".m4a", ".zip", ".gz"}
PDF_IMAGE_LIMIT = 3 * 1024 * 1024
STANDALONE_VERIFIER = Path(__file__).parent / "standalone" / "verify_export.py"


@dataclass
class MediaView:
    type: str
    label: str
    href: str | None = None
    rel: str | None = None
    size: int = 0
    name: str | None = None
    note: str | None = None
    sha256: str | None = None

    @property
    def size_label(self) -> str:
        return human_size(self.size) if self.size else ""

    @property
    def ext(self) -> str:
        return Path(self.href).suffix.lower() if self.href else ""

    @property
    def pdf_image(self) -> bool:
        return self.type == "photo" and self.href is not None and self.size <= PDF_IMAGE_LIMIT


@dataclass
class ReplyView:
    sender: str
    text: str
    anchor: str


@dataclass
class EditView:
    at: str
    at_utc: str
    text: str | None


@dataclass
class MessageView:
    message_id: int
    outgoing: bool
    sender: str
    sender_id: int | None
    color: str
    time: str
    full_time: str
    utc_time: str
    day: str
    text: str | None
    reply: ReplyView | None
    media: MediaView | None
    edited_at: str | None
    edited_at_utc: str | None
    deleted_at: str | None
    deleted_at_utc: str | None
    read_at: str | None = None
    read_at_utc: str | None = None
    read_seq: int | None = None
    fwd: str | None = None
    reactions: str = ""
    service: str | None = None
    record_hash: str | None = None
    first_seq: int | None = None
    edits: list[EditView] = field(default_factory=list)
    new_day: bool = False
    show_sender: bool = True


@dataclass
class Interlocutor:
    user_id: int
    name: str | None
    username: str | None
    phone: str | None
    bio: str | None
    is_bot: bool
    known_names: list[str]
    known_usernames: list[str]
    known_phones: list[str]
    history: list[dict]


@dataclass
class ExportContext:
    chat_id: int
    chat: object
    title: str
    views: list[MessageView]
    entries: list[Entry]
    interlocutor: Interlocutor | None
    head: tuple[int, str]
    anchors: list
    chain_from: int = 1


def css_string(value: object) -> Markup:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ").replace("\n", " ")
    return Markup(text)


def slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return slug[:40] or fallback


def day_title(value: datetime) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def utc_label(value: str | None) -> str | None:
    dt = from_iso(value) if value else None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if dt else None


def snippet(row, limit: int = 90) -> str:
    text = (row["text"] or row["service_action"] or "").replace("\n", " ").strip()
    if not text and row["media_type"]:
        text = media_label(row["media_type"]).capitalize()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def media_arcname(vault: Vault, rel: str) -> str:
    return f"media/{vault.logical_name(rel)}"


class Exporter:
    def __init__(
        self,
        db: Database,
        config: Config,
        vault: Vault | None = None,
        evidence: EvidenceLog | None = None,
        signer: Signer | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.vault = vault or Vault(config.data_dir, KeyRing(None))
        self.evidence = evidence or EvidenceLog(db)
        self.signer = signer or Signer.load_or_create(config.keys_dir, KeyRing(None))
        self.env = Environment(
            loader=PackageLoader("tgbackup", "templates"),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["css"] = css_string

    async def export(self, chat_id: int) -> list[Path]:
        context = await self.collect(chat_id)
        parts = self.split_parts(context.views)
        part_entries = [self.entries_for_part(context, part) for part in parts]
        start = min((e.seq for entries in part_entries for e in entries), default=context.head[0] or 1)
        chain = await self.chain_rows(start, context.head[0])
        return await asyncio.to_thread(self.write_parts, context, parts, part_entries, chain)

    async def collect(self, chat_id: int) -> ExportContext:
        chat = await self.db.get_chat(chat_id)
        rows = await self.db.chat_messages(chat_id)
        edits = await self.db.chat_edits(chat_id)
        entries = await self.evidence.for_chat(chat_id)
        head = await self.evidence.head()
        anchors = await self.db.anchors()
        interlocutor = await self.interlocutor(chat_id, chat, rows)
        title = (chat["title"] if chat else None) or str(chat_id)
        return ExportContext(
            chat_id=chat_id,
            chat=chat,
            title=title,
            views=self.build_views(chat, rows, edits),
            entries=entries,
            interlocutor=interlocutor,
            head=head,
            anchors=anchors,
        )

    async def interlocutor(self, chat_id: int, chat, rows) -> Interlocutor | None:
        if chat is None or chat["type"] != "private":
            return None
        user = await self.db.get_user(chat_id)
        history = await self.db.profile_history(chat_id)
        names: list[str] = []
        usernames: list[str] = []
        phones: list[str] = []

        def add(bucket: list[str], value: str | None) -> None:
            if value and value not in bucket:
                bucket.append(value)

        for item in history:
            add(names, full_name(item["first_name"], item["last_name"]))
            add(usernames, item["username"])
            add(phones, item["phone"])
        if user is not None:
            add(names, full_name(user["first_name"], user["last_name"]))
            add(usernames, user["username"])
            add(phones, user["phone"])
        for row in rows:
            if row["sender_id"] == chat_id:
                add(names, row["sender_name"])
                add(usernames, row["sender_username"])

        tz = self.config.timezone
        changes = []
        for item in history:
            diff = json.loads(item["changes"])
            changes.append(
                {
                    "at": fmt_dt(item["observed_at"], tz),
                    "at_utc": utc_label(item["observed_at"]),
                    "source": item["source"],
                    "changes": [
                        (field_name, old, new) for field_name, (old, new) in diff.items() if field_name != "photo_id"
                    ]
                    + ([("photo", None, "новое фото" if item["photo_id"] else "удалено/скрыто")]
                       if "photo_id" in diff else []),
                }
            )
        return Interlocutor(
            user_id=chat_id,
            name=full_name(user["first_name"], user["last_name"]) if user else (chat["title"] if chat else None),
            username=user["username"] if user else chat["username"],
            phone=user["phone"] if user else None,
            bio=user["bio"] if user else None,
            is_bot=bool(user["is_bot"]) if user else False,
            known_names=names,
            known_usernames=usernames,
            known_phones=phones,
            history=changes,
        )

    def build_views(self, chat, rows, edits) -> list[MessageView]:
        tz = self.config.timezone
        by_id = {row["message_id"]: row for row in rows}
        is_private = chat is not None and chat["type"] == "private"
        views: list[MessageView] = []
        previous_day = None
        previous_sender = None

        for row in rows:
            sent = from_iso(row["date"]).astimezone(tz)
            day = sent.date()
            reply = None
            if row["reply_to"] and row["reply_to"] in by_id:
                target = by_id[row["reply_to"]]
                reply = ReplyView(sender_label(target), snippet(target), f"m{target['message_id']}")
            elif row["reply_to"]:
                reply = ReplyView("", f"Сообщение #{row['reply_to']} не сохранено", f"m{row['reply_to']}")

            fwd = None
            if row["fwd_from_name"] or row["fwd_from_id"]:
                fwd = row["fwd_from_name"] or str(row["fwd_from_id"])
                if row["fwd_date"]:
                    fwd += f" · {fmt_dt(row['fwd_date'], tz, '%d.%m.%Y %H:%M')}"

            sender_key = (row["outgoing"], row["sender_id"])
            service = row["service_action"]
            view = MessageView(
                message_id=row["message_id"],
                outgoing=bool(row["outgoing"]),
                sender=sender_label(row),
                sender_id=row["sender_id"],
                color=PALETTE[(row["sender_id"] or 0) % len(PALETTE)],
                time=sent.strftime("%H:%M"),
                full_time=fmt_dt(sent, tz),
                utc_time=utc_label(row["date"]),
                day=day_title(sent),
                text=row["text"],
                reply=reply,
                media=None if service else self.media_view(row),
                edited_at=fmt_dt(row["edit_date"], tz) if row["edited"] else None,
                edited_at_utc=utc_label(row["edit_date"]) if row["edited"] else None,
                deleted_at=fmt_dt(row["deleted_at"], tz) if row["deleted"] else None,
                deleted_at_utc=utc_label(row["deleted_at"]) if row["deleted"] else None,
                read_at=fmt_dt(row["read_at"], tz) if row["read_at"] else None,
                read_at_utc=utc_label(row["read_at"]),
                read_seq=row["read_seq"],
                fwd=fwd,
                reactions=reactions_label(row["reactions"]),
                service=service,
                record_hash=row["record_hash"],
                first_seq=row["first_seq"],
                edits=[
                    EditView(fmt_dt(e["edited_at"], tz), utc_label(e["edited_at"]), e["old_text"])
                    for e in edits.get(row["message_id"], [])
                ],
                new_day=day != previous_day,
                show_sender=not service and not is_private and (day != previous_day or sender_key != previous_sender),
            )
            views.append(view)
            previous_day = day
            previous_sender = None if service else sender_key
        return views

    def media_view(self, row) -> MediaView | None:
        if not row["media_type"]:
            return None
        view = MediaView(
            type=row["media_type"],
            label=media_label(row["media_type"]),
            note=row["media_note"],
            sha256=row["media_sha256"],
        )
        if row["media_path"]:
            if self.vault.exists(row["media_path"]):
                view.rel = row["media_path"]
                view.size = row["media_size"] or self.vault.path(row["media_path"]).stat().st_size
                view.href = media_arcname(self.vault, row["media_path"])
                view.name = Path(view.href).name
            else:
                view.note = view.note or "файл не найден на диске"
        elif row["media_size"]:
            view.size = row["media_size"]
        return view

    def split_parts(self, views: list[MessageView]) -> list[list[MessageView]]:
        budget = self.config.export_part_bytes
        parts: list[list[MessageView]] = [[]]
        used = 0
        for view in views:
            media = view.media
            size = media.size if media and media.rel else 0
            if size > budget:
                media.note = f"файл {media.size_label} не поместился в архив, хранится на сервере"
                media.rel = media.href = None
                size = 0
            if media and media.pdf_image and self.config.export_pdf:
                size *= 2
            overhead = 600 + len(view.text or "") * 2
            if used + size + overhead > budget and parts[-1]:
                parts.append([])
                used = 0
            parts[-1].append(view)
            used += size + overhead
        return parts

    def entries_for_part(self, context: ExportContext, part: list[MessageView]) -> list[Entry]:
        ids = {view.message_id for view in part}
        selected = []
        for entry in context.entries:
            if entry.message_id in ids or entry.event == EVENT_PROFILE:
                selected.append(entry)
            elif entry.event in (EVENT_DELETE, EVENT_READ):
                data = json.loads(entry.payload).get("data", {})
                if ids.intersection(data.get("message_ids", [])):
                    selected.append(entry)
        return selected

    async def chain_rows(self, start: int, end: int) -> list[tuple]:
        rows = []
        if end <= 0:
            return rows
        async for entry in self.evidence.iter_all(start, end):
            rows.append((entry.seq, entry.prev_hash, entry.record_hash, entry.entry_hash))
        return rows

    def write_parts(
        self,
        context: ExportContext,
        parts: list[list[MessageView]],
        part_entries: list[list[Entry]],
        chain: list[tuple],
    ) -> list[Path]:
        self.config.exports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        base = f"{slugify(context.title, 'chat')}_{context.chat_id}_{stamp.astimezone(self.config.timezone):%Y%m%d_%H%M%S}"
        total = len(parts)
        paths = []
        for index, (part, entries) in enumerate(zip(parts, part_entries), 1):
            name = f"{base}_part{index}.zip" if total > 1 else f"{base}.zip"
            path = self.config.exports_dir / name
            self.write_part(path, context, part, entries, chain, index, total, stamp)
            paths.append(path)
        return paths

    def write_part(
        self,
        path: Path,
        context: ExportContext,
        part: list[MessageView],
        entries: list[Entry],
        chain: list[tuple],
        index: int,
        total: int,
        stamp: datetime,
    ) -> None:
        tz = self.config.timezone
        files: dict[str, bytes] = {}
        media_files: dict[str, str] = {}

        for view in part:
            if view.media and view.media.rel:
                media_files[view.media.href] = view.media.rel
        for entry in entries:
            if entry.media_path and self.vault.exists(entry.media_path):
                media_files.setdefault(media_arcname(self.vault, entry.media_path), entry.media_path)

        if part:
            part[0].new_day = True
            part[0].show_sender = not part[0].service and (context.chat is None or context.chat["type"] != "private")

        chain_slice = [row for row in chain if row[0] >= (entries[0].seq if entries else (chain[0][0] if chain else 0))]
        head_seq, head_hash = (chain_slice[-1][0], chain_slice[-1][3]) if chain_slice else context.head
        anchors = [a for a in context.anchors if entries and a["seq"] >= entries[0].seq and a["seq"] <= head_seq]
        records_digest = hashlib.sha256("\n".join(e.entry_hash for e in entries).encode()).hexdigest()

        common = {
            "title": context.title,
            "username": context.chat["username"] if context.chat else None,
            "chat_type": context.chat["type"] if context.chat else "unknown",
            "chat_id": context.chat_id,
            "messages": part,
            "part": index,
            "total": total,
            "stats": {
                "messages": len(part),
                "deleted": sum(1 for v in part if v.deleted_at),
                "edited": sum(1 for v in part if v.edited_at),
                "media": sum(1 for v in part if v.media and v.media.rel),
            },
            "generated": fmt_dt(stamp, tz),
            "generated_utc": stamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "timezone": str(tz),
            "period": (part[0].full_time, part[-1].full_time) if part else None,
            "interlocutor": context.interlocutor,
            "records_digest": records_digest,
            "head_seq": head_seq,
            "head_hash": head_hash,
            "fingerprint": self.signer.fingerprint,
            "version": __version__,
            "anchors": anchors,
        }
        files["chat.html"] = self.env.get_template("chat.html").render(**common).encode()

        records_lines = []
        arc_by_rel = {rel: arc for arc, rel in media_files.items()}
        for entry in entries:
            item = entry.to_json()
            if entry.media_path and entry.media_path in arc_by_rel:
                item["media_file"] = arc_by_rel[entry.media_path]
            records_lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        files["evidence/records.jsonl"] = ("\n".join(records_lines) + "\n").encode()

        chain_buffer = io.StringIO()
        writer = csv.writer(chain_buffer, lineterminator="\n")
        writer.writerow(["seq", "prev_hash", "record_hash", "entry_hash"])
        writer.writerows(chain_slice)
        files["evidence/chain.csv"] = chain_buffer.getvalue().encode()

        anchor_meta = []
        for anchor in anchors:
            meta = {key: anchor[key] for key in anchor.keys() if key not in {"tsa_error", "ots_error"}}
            for key in ("anchor_file", "tsr_file", "ots_file"):
                rel = anchor[key]
                source = self.config.data_dir / rel if rel else None
                if source is not None and source.exists():
                    arc = f"evidence/anchors/{source.name}"
                    files[arc] = source.read_bytes()
                    meta[key] = arc
                else:
                    meta[key] = None
            anchor_meta.append(meta)
        for cert in ("freetsa_cacert.pem", "freetsa_tsa.crt"):
            source = self.config.anchors_dir / cert
            if anchors and source.exists():
                files[f"evidence/anchors/{cert}"] = source.read_bytes()

        files["verify_export.py"] = STANDALONE_VERIFIER.read_bytes()
        files["README.txt"] = self.env.get_template("export_readme.txt").render(**common).encode()

        if self.config.export_pdf:
            pdf = self.render_pdf(common, part, media_files)
            if pdf is not None:
                files["chat.pdf"] = pdf

        file_hashes = {name: sha256_hex(data) for name, data in files.items()}
        media_plain: dict[str, bytes] = {}
        for arc, rel in media_files.items():
            data = self.vault.read(rel)
            media_plain[arc] = data
            file_hashes[arc] = sha256_hex(data)

        interlocutor = context.interlocutor
        manifest = {
            "format": "tgbackup-evidence-export/1",
            "program": {"name": "tgbackup", "version": __version__},
            "exported_at_utc": stamp.isoformat(),
            "timezone": str(tz),
            "part": index,
            "parts": total,
            "chat": {
                "id": context.chat_id,
                "title": context.title,
                "type": context.chat["type"] if context.chat else None,
                "username": context.chat["username"] if context.chat else None,
            },
            "interlocutor": None
            if interlocutor is None
            else {
                "user_id": interlocutor.user_id,
                "name": interlocutor.name,
                "username": interlocutor.username,
                "phone": interlocutor.phone,
                "known_names": interlocutor.known_names,
                "known_usernames": interlocutor.known_usernames,
                "known_phones": interlocutor.known_phones,
            },
            "chain": {
                "hash_rule": "record_hash = sha256(payload); entry_hash = sha256(f'{seq}:{prev_hash}:{record_hash}')",
                "first_seq": chain_slice[0][0] if chain_slice else None,
                "head_seq": head_seq,
                "head_hash": head_hash,
                "records": len(entries),
            },
            "records_digest": records_digest,
            "messages": [
                {
                    "message_id": view.message_id,
                    "date_utc": view.utc_time,
                    "sender_id": view.sender_id,
                    "outgoing": view.outgoing,
                    "first_seq": view.first_seq,
                    "record_hash": view.record_hash,
                    "edited_at_utc": view.edited_at_utc,
                    "deleted_at_utc": view.deleted_at_utc,
                    "read_not_later_than_utc": view.read_at_utc,
                    "read_seq": view.read_seq,
                    "media_sha256": view.media.sha256 if view.media else None,
                    "media_file": view.media.href if view.media and view.media.rel else None,
                    "evidence_seqs": [e.seq for e in entries if e.message_id == view.message_id],
                }
                for view in part
            ],
            "anchors": anchor_meta,
            "files": dict(sorted(file_hashes.items())),
            "signature": {
                "algorithm": "Ed25519",
                "public_key_file": "public_key.pem",
                "signature_file": "manifest.sig",
                "public_key_sha256": self.signer.fingerprint,
            },
        }
        manifest_bytes = canonical_json(manifest).encode()
        signature = self.signer.sign(manifest_bytes)

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            for arc, data in media_plain.items():
                compress = zipfile.ZIP_STORED if Path(arc).suffix.lower() in STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
                archive.writestr(arc, data, compress_type=compress)
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("manifest.sig", signature)
            archive.writestr("public_key.pem", self.signer.public_pem)

    def render_pdf(self, common: dict, part: list[MessageView], media_files: dict[str, str]) -> bytes | None:
        try:
            from weasyprint import HTML
        except Exception as exc:
            log.warning("PDF export disabled: %s", exc)
            return None
        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(dir=self.config.tmp_dir))
        try:
            for view in part:
                if view.media and view.media.pdf_image and view.media.href in media_files:
                    target = workdir / view.media.href
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self.vault.read(media_files[view.media.href]))
            html = self.env.get_template("chat_pdf.html").render(**common)
            return HTML(string=html, base_url=str(workdir)).write_pdf()
        except Exception:
            log.exception("PDF rendering failed")
            return None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

