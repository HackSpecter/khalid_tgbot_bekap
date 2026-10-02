from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from asn1crypto import tsp

from . import __version__
from .config import Config
from .db import Database, utcnow
from .evidence import EVENT_ANCHOR, EvidenceLog

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 60
USER_AGENT = f"tgbackup/{__version__}"


@dataclass
class TsaResult:
    ok: bool
    gen_time: str | None = None
    serial: int | None = None
    tsa: str | None = None
    error: str | None = None


@dataclass
class OtsResult:
    status: str
    blocks: list[int] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    block_time: str | None = None
    verified: bool | None = None
    error: str | None = None


def build_tsq(digest: bytes) -> tuple[bytes, int]:
    nonce = int.from_bytes(os.urandom(8), "big")
    request = tsp.TimeStampReq(
        {
            "version": 1,
            "message_imprint": {"hash_algorithm": {"algorithm": "sha256"}, "hashed_message": digest},
            "nonce": nonce,
            "cert_req": True,
        }
    )
    return request.dump(), nonce


def http_post(url: str, data: bytes, content_type: str) -> bytes:
    request = urllib.request.Request(url, data=data, headers={"Content-Type": content_type, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def parse_tsr(data: bytes, digest: bytes, nonce: int | None = None) -> TsaResult:
    try:
        response = tsp.TimeStampResp.load(data)
        status = response["status"]["status"].native
        if status not in {"granted", "granted_with_mods"}:
            return TsaResult(False, error=f"TSA status: {status}")
        info = response["time_stamp_token"]["content"]["encap_content_info"]["content"].parsed
        if info["message_imprint"]["hashed_message"].native != digest:
            return TsaResult(False, error="message imprint mismatch")
        if nonce is not None and info["nonce"].native != nonce:
            return TsaResult(False, error="nonce mismatch")
        tsa_name = info["tsa"].native
        if isinstance(tsa_name, dict):
            tsa_name = tsa_name.get("common_name") or json.dumps(tsa_name, ensure_ascii=False)
        return TsaResult(
            True,
            gen_time=info["gen_time"].native.astimezone(timezone.utc).isoformat(),
            serial=info["serial_number"].native,
            tsa=str(tsa_name) if tsa_name else None,
        )
    except Exception as exc:
        return TsaResult(False, error=f"cannot parse TSR: {exc}")


def openssl_ts_verify(data_file: Path, tsr_file: Path, ca_file: Path, tsa_cert: Path) -> tuple[bool | None, str]:
    openssl = shutil.which("openssl")
    if not openssl or not ca_file.exists() or not tsa_cert.exists():
        return None, "openssl или сертификаты TSA недоступны"
    result = subprocess.run(
        [openssl, "ts", "-verify", "-data", str(data_file), "-in", str(tsr_file),
         "-CAfile", str(ca_file), "-untrusted", str(tsa_cert)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and "Verification: OK" in output, output


def inspect_ots(ots_file: Path, expected_digest: bytes | None = None) -> OtsResult:
    try:
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.serialize import StreamDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
    except ImportError:
        return OtsResult("unknown", error="opentimestamps не установлен")
    try:
        with open(ots_file, "rb") as fh:
            detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))
    except Exception as exc:
        return OtsResult("invalid", error=f"cannot parse .ots: {exc}")
    if expected_digest is not None and detached.file_digest != expected_digest:
        return OtsResult("invalid", error=".ots относится к другому файлу")
    result = OtsResult("pending")
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            result.blocks.append(attestation.height)
            result.status = "complete"
        elif isinstance(attestation, PendingAttestation):
            result.pending.append(attestation.uri)
    return result


def bitcoin_attestations(ots_file: Path) -> list[tuple[int, bytes]]:
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.serialize import StreamDeserializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile

    with open(ots_file, "rb") as fh:
        detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))
    return [
        (attestation.height, msg)
        for msg, attestation in detached.timestamp.all_attestations()
        if isinstance(attestation, BitcoinBlockHeaderAttestation)
    ]


def verify_ots_with_explorer(ots_file: Path, explorer: str = "https://blockstream.info/api") -> OtsResult:
    result = inspect_ots(ots_file)
    if result.status != "complete":
        return result
    try:
        for height, merkle in bitcoin_attestations(ots_file):
            block_hash = http_get(f"{explorer}/block-height/{height}").decode().strip()
            block = json.loads(http_get(f"{explorer}/block/{block_hash}"))
            if block["merkle_root"] == merkle[::-1].hex():
                result.verified = True
                result.block_time = datetime.fromtimestamp(block["timestamp"], timezone.utc).isoformat()
                return result
        result.verified = False
        result.error = "merkle root блока не совпадает"
    except Exception as exc:
        result.verified = None
        result.error = f"проводник блокчейна недоступен: {exc}"
    return result


def run_ots(*args: str) -> tuple[int, str]:
    binary = shutil.which("ots")
    if not binary:
        return 127, "ots CLI не найден"
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=300)
    return result.returncode, (result.stdout + result.stderr).strip()


def anchor_text(seq: int, entry_hash: str, created: str) -> str:
    return (
        "tgbackup evidence chain anchor\n"
        f"program: tgbackup {__version__}\n"
        f"created_utc: {created}\n"
        f"seq: {seq}\n"
        f"entry_hash: {entry_hash}\n"
        "hash_rule: entry_hash = sha256(f\"{seq}:{prev_hash}:{record_hash}\")\n"
    )


def parse_anchor_text(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            values[key.strip()] = value.strip()
    return values


class Anchorer:
    def __init__(self, config: Config, db: Database, evidence: EvidenceLog) -> None:
        self.config = config
        self.db = db
        self.evidence = evidence
        self._lock = asyncio.Lock()

    @property
    def ca_file(self) -> Path:
        return self.config.anchors_dir / "freetsa_cacert.pem"

    @property
    def tsa_cert(self) -> Path:
        return self.config.anchors_dir / "freetsa_tsa.crt"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.config.data_dir).as_posix()

    async def ensure_tsa_certs(self) -> None:
        targets = [self.ca_file, self.tsa_cert]
        for url, target in zip(self.config.tsa_cert_urls, targets):
            if target.exists():
                continue
            try:
                data = await asyncio.to_thread(http_get, url)
                target.write_bytes(data)
            except Exception as exc:
                log.warning("Cannot download TSA certificate %s: %s", url, exc)

    async def anchor_now(self, force: bool = False) -> dict | None:
        async with self._lock:
            seq, entry_hash = await self.evidence.head()
            if seq == 0:
                return None
            last = await self.db.last_anchor()
            if last is not None and last["seq"] >= seq and not force:
                return None
            if last is not None and last["seq"] == seq - 1 and not force:
                previous = await self.evidence.entry_at(seq)
                if previous is not None and previous.event == EVENT_ANCHOR:
                    return None

            created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            stem = f"anchor_{seq:010d}_{created.replace(':', '').replace('-', '')}"
            anchor_file = self.config.anchors_dir / f"{stem}.txt"
            content = anchor_text(seq, entry_hash, created).encode()
            anchor_file.write_bytes(content)
            digest = hashlib.sha256(content).digest()

            record = {
                "created_at": utcnow(),
                "seq": seq,
                "entry_hash": entry_hash,
                "anchor_file": self.rel(anchor_file),
                "anchor_sha256": digest.hex(),
            }

            if self.config.tsa_enabled:
                await self.ensure_tsa_certs()
                tsa = await self.request_tsa(anchor_file, digest)
                record["tsr_file"] = tsa.get("tsr_file")
                record["tsa_time"] = tsa.get("tsa_time")
                record["tsa_error"] = tsa.get("error")

            if self.config.ots_enabled:
                code, output = await asyncio.to_thread(run_ots, "stamp", str(anchor_file))
                ots_file = Path(f"{anchor_file}.ots")
                if code == 0 and ots_file.exists():
                    record["ots_file"] = self.rel(ots_file)
                    record["ots_status"] = "pending"
                else:
                    record["ots_status"] = "error"
                    record["ots_error"] = output[-500:]

            anchor_id = await self.db.add_anchor(record)
            await self.evidence.append(
                EVENT_ANCHOR,
                data={
                    "anchor_id": anchor_id,
                    "anchored_seq": seq,
                    "anchored_entry_hash": entry_hash,
                    "anchor_sha256": record["anchor_sha256"],
                    "tsa_time": record.get("tsa_time"),
                    "tsr_sha256": self.file_sha256(record.get("tsr_file")),
                    "ots_status": record.get("ots_status"),
                },
            )
            log.info("Anchored seq=%s tsa=%s ots=%s", seq, record.get("tsa_time"), record.get("ots_status"))
            return {**record, "id": anchor_id}

    def file_sha256(self, rel: str | None) -> str | None:
        if not rel:
            return None
        path = self.config.data_dir / rel
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    async def request_tsa(self, anchor_file: Path, digest: bytes) -> dict:
        tsq, nonce = build_tsq(digest)
        try:
            data = await asyncio.to_thread(http_post, self.config.tsa_url, tsq, "application/timestamp-query")
        except Exception as exc:
            log.warning("TSA request failed: %s", exc)
            return {"error": str(exc)[:500]}
        result = parse_tsr(data, digest, nonce)
        if not result.ok:
            return {"error": result.error}
        tsr_file = Path(f"{anchor_file}.tsr")
        tsr_file.write_bytes(data)
        Path(f"{anchor_file}.tsq").write_bytes(tsq)
        return {"tsr_file": self.rel(tsr_file), "tsa_time": result.gen_time}

    async def retry_failed_tsa(self) -> None:
        if not self.config.tsa_enabled:
            return
        for anchor in await self.db.anchors():
            if anchor["tsr_file"]:
                continue
            anchor_file = self.config.data_dir / anchor["anchor_file"]
            if not anchor_file.exists():
                continue
            await self.ensure_tsa_certs()
            tsa = await self.request_tsa(anchor_file, bytes.fromhex(anchor["anchor_sha256"]))
            await self.db.update_anchor(
                anchor["id"], tsr_file=tsa.get("tsr_file"), tsa_time=tsa.get("tsa_time"), tsa_error=tsa.get("error")
            )

    async def upgrade_pending(self) -> int:
        if not self.config.ots_enabled:
            return 0
        upgraded = 0
        for anchor in await self.db.anchors():
            if anchor["ots_status"] == "complete":
                continue
            anchor_file = self.config.data_dir / anchor["anchor_file"]
            ots_file = Path(f"{anchor_file}.ots")
            if not ots_file.exists():
                code, output = await asyncio.to_thread(run_ots, "stamp", str(anchor_file))
                if code != 0 or not ots_file.exists():
                    await self.db.update_anchor(anchor["id"], ots_status="error", ots_error=output[-500:],
                                                ots_checked_at=utcnow())
                    continue
                await self.db.update_anchor(anchor["id"], ots_file=self.rel(ots_file), ots_status="pending")
            await asyncio.to_thread(run_ots, "upgrade", str(ots_file))
            info = await asyncio.to_thread(inspect_ots, ots_file, bytes.fromhex(anchor["anchor_sha256"]))
            fields = {"ots_checked_at": utcnow(), "ots_status": info.status, "ots_error": info.error}
            if info.status == "complete":
                verified = await asyncio.to_thread(verify_ots_with_explorer, ots_file)
                fields["ots_block"] = min(info.blocks)
                fields["ots_block_time"] = verified.block_time
                upgraded += 1
                log.info("OTS anchor %s confirmed in block %s", anchor["id"], fields["ots_block"])
            await self.db.update_anchor(anchor["id"], **fields)
        return upgraded

    async def run_scheduler(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Anchor scheduler failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        local = now.astimezone(self.config.timezone)
        last_daily = await self.db.kv_get("anchor_daily_date")
        if local.hour >= self.config.anchor_hour and last_daily != local.date().isoformat():
            await self.anchor_now()
            await self.retry_failed_tsa()
            await self.db.kv_set("anchor_daily_date", local.date().isoformat())

        last_upgrade = await self.db.kv_get("ots_upgrade_at")
        due = last_upgrade is None or now - datetime.fromisoformat(last_upgrade) >= timedelta(
            hours=self.config.ots_upgrade_hours
        )
        if due:
            await self.upgrade_pending()
            await self.db.kv_set("ots_upgrade_at", now.isoformat())
