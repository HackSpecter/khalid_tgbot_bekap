from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"TGBKENC1"
NONCE_SIZE = 12
ENC_SUFFIX = ".enc"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_master_key(value: str) -> bytes:
    value = value.strip()
    try:
        raw = bytes.fromhex(value)
        if len(raw) >= 32:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) >= 32:
            return raw
    except ValueError:
        pass
    if len(value) >= 32:
        return value.encode()
    raise ValueError("ENCRYPTION_KEY must contain at least 32 bytes of entropy")


class KeyRing:
    def __init__(self, master: bytes | None) -> None:
        self.master = master

    @classmethod
    def from_secret(cls, secret: str | None) -> "KeyRing":
        return cls(decode_master_key(secret) if secret else None)

    @property
    def enabled(self) -> bool:
        return self.master is not None

    def derive(self, label: str, length: int = 32) -> bytes:
        if self.master is None:
            raise RuntimeError("Encryption key is not configured")
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=b"tgbackup/v2", info=label.encode()).derive(
            self.master
        )

    @property
    def db_key_hex(self) -> str | None:
        return self.derive("sqlcipher").hex() if self.enabled else None

    @property
    def media_key(self) -> bytes | None:
        return self.derive("media-aes-gcm") if self.enabled else None

    @property
    def signing_passphrase(self) -> bytes | None:
        return self.derive("ed25519-pem") if self.enabled else None


def is_encrypted_blob(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_blob(key: bytes, data: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad or None)


def decrypt_blob(key: bytes | None, blob: bytes, aad: bytes = b"") -> bytes:
    if not is_encrypted_blob(blob):
        return blob
    if key is None:
        raise RuntimeError("File is encrypted but ENCRYPTION_KEY is not configured")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE_SIZE :], aad or None)


class Vault:
    def __init__(self, root: Path, keyring: KeyRing) -> None:
        self.root = Path(root)
        self.key = keyring.media_key

    def resolve(self, rel: str) -> str:
        if not (self.root / rel).exists() and (self.root / f"{rel}{ENC_SUFFIX}").exists():
            return f"{rel}{ENC_SUFFIX}"
        return rel

    def path(self, rel: str) -> Path:
        return self.root / self.resolve(rel)

    def logical_name(self, rel: str) -> str:
        name = Path(rel).name
        return name[: -len(ENC_SUFFIX)] if name.endswith(ENC_SUFFIX) else name

    def write(self, rel: str, data: bytes) -> tuple[str, str, int]:
        digest = sha256_hex(data)
        if self.key is not None:
            rel = rel if rel.endswith(ENC_SUFFIX) else rel + ENC_SUFFIX
            payload = encrypt_blob(self.key, data, self._aad(rel))
        else:
            payload = data
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
        return rel, digest, len(data)

    def read(self, rel: str) -> bytes:
        rel = self.resolve(rel)
        blob = (self.root / rel).read_bytes()
        return decrypt_blob(self.key, blob, self._aad(rel))

    def exists(self, rel: str | None) -> bool:
        return bool(rel) and self.path(rel).is_file()

    def _aad(self, rel: str) -> bytes:
        return Path(rel).name.encode()

    def encrypt_existing(self, rel: str) -> tuple[str, str]:
        source = self.path(rel)
        data = source.read_bytes()
        if is_encrypted_blob(data) or self.key is None:
            plain = self.read(rel)
            return rel, sha256_hex(plain)
        new_rel, digest, _ = self.write(rel, data)
        if new_rel != rel:
            source.unlink()
        return new_rel, digest


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.private_key = private_key

    @classmethod
    def load_or_create(cls, keys_dir: Path, keyring: KeyRing) -> "Signer":
        keys_dir.mkdir(parents=True, exist_ok=True)
        private_path = keys_dir / "ed25519_private.pem"
        public_path = keys_dir / "ed25519_public.pem"
        passphrase = keyring.signing_passphrase
        if private_path.exists():
            key = serialization.load_pem_private_key(private_path.read_bytes(), password=passphrase)
        else:
            key = Ed25519PrivateKey.generate()
            encryption = (
                serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
            )
            private_path.write_bytes(
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
            )
            os.chmod(private_path, 0o600)
        signer = cls(key)
        public_path.write_bytes(signer.public_pem)
        return signer

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    @property
    def public_pem(self) -> bytes:
        return self.public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    @property
    def fingerprint(self) -> str:
        raw = self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return sha256_hex(raw)

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


def verify_signature(public_pem: bytes, data: bytes, signature: bytes) -> bool:
    key = serialization.load_pem_public_key(public_pem)
    try:
        key.verify(signature, data)
        return True
    except Exception:
        return False
