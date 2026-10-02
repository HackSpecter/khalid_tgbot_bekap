import pytest

from tgbackup.crypto import ENC_SUFFIX, KeyRing, Signer, Vault, decode_master_key, verify_signature

SECRET = "a" * 64


def test_keyring_derives_distinct_stable_keys():
    ring = KeyRing.from_secret(SECRET)
    assert ring.db_key_hex == KeyRing.from_secret(SECRET).db_key_hex
    assert len({ring.db_key_hex, ring.media_key.hex(), ring.signing_passphrase.hex()}) == 3


def test_short_key_rejected():
    with pytest.raises(ValueError):
        decode_master_key("short")


def test_vault_encrypts_and_hashes_plaintext(tmp_path):
    vault = Vault(tmp_path, KeyRing.from_secret(SECRET))
    rel, digest, size = vault.write("media/1/1_1.jpg", b"secret photo")

    assert rel.endswith(ENC_SUFFIX)
    assert b"secret photo" not in (tmp_path / rel).read_bytes()
    assert vault.read(rel) == b"secret photo"
    assert vault.read("media/1/1_1.jpg") == b"secret photo"
    assert size == 12 and len(digest) == 64
    assert vault.logical_name(rel) == "1_1.jpg"


def test_vault_detects_swapped_or_modified_files(tmp_path):
    vault = Vault(tmp_path, KeyRing.from_secret(SECRET))
    a, _, _ = vault.write("media/a.jpg", b"A")
    b, _, _ = vault.write("media/b.jpg", b"B")
    (tmp_path / a).write_bytes((tmp_path / b).read_bytes())
    with pytest.raises(Exception):
        vault.read(a)


def test_vault_encrypts_legacy_file(tmp_path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "old.jpg").write_bytes(b"legacy")
    vault = Vault(tmp_path, KeyRing.from_secret(SECRET))
    rel, digest = vault.encrypt_existing("media/old.jpg")
    assert rel == "media/old.jpg.enc"
    assert not (tmp_path / "media" / "old.jpg").exists()
    assert vault.read("media/old.jpg") == b"legacy"


def test_signer_persists_encrypted_key(tmp_path):
    ring = KeyRing.from_secret(SECRET)
    signer = Signer.load_or_create(tmp_path, ring)
    pem = (tmp_path / "ed25519_private.pem").read_bytes()
    assert b"ENCRYPTED" in pem
    again = Signer.load_or_create(tmp_path, ring)
    assert again.fingerprint == signer.fingerprint
    signature = again.sign(b"manifest")
    assert verify_signature(signer.public_pem, b"manifest", signature)
    assert not verify_signature(signer.public_pem, b"manifest2", signature)
