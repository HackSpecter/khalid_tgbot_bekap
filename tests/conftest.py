import pytest
import pytest_asyncio

from tgbackup.config import Config
from tgbackup.crypto import KeyRing, Signer, Vault
from tgbackup.db import Database
from tgbackup.evidence import EvidenceLog
from tgbackup.exporter import Exporter


class FakeNotifier:
    def __init__(self):
        self.texts = []
        self.files = []
        self.media = []
        self.exports = []
        self.buttons = []

    async def send_text(self, text, buttons=None):
        self.texts.append(text)
        self.buttons.append(buttons)

    async def send_file(self, path, caption="", **kwargs):
        self.files.append((path, caption))

    async def send_media(self, rel_path, caption="", media_type=None, buttons=None):
        self.media.append((rel_path, caption, media_type))
        self.buttons.append(buttons)

    async def send_export(self, parts, title, intro="", buttons=None):
        self.exports.append((parts, title, intro))


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        api_id=1,
        api_hash="hash",
        session_name="test",
        data_dir=tmp_path,
        notify_bot_token=None,
        owner_id=None,
        tsa_enabled=False,
        ots_enabled=False,
        export_pdf=False,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def keyring():
    return KeyRing(None)


@pytest_asyncio.fixture
async def db(config):
    database = Database(config.db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def evidence(db):
    return EvidenceLog(db)


@pytest.fixture
def vault(config, keyring):
    return Vault(config.data_dir, keyring)


@pytest.fixture
def signer(config, keyring):
    return Signer.load_or_create(config.keys_dir, keyring)


@pytest.fixture
def exporter(db, config, vault, evidence, signer):
    return Exporter(db, config, vault, evidence, signer)


@pytest.fixture
def notifier():
    return FakeNotifier()
