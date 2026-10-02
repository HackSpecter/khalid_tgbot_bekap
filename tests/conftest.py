import pytest
import pytest_asyncio

from tgbackup.config import Config
from tgbackup.db import Database


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        api_id=1,
        api_hash="hash",
        session_name="test",
        data_dir=tmp_path,
        notify_bot_token=None,
        owner_id=None,
    )
    cfg.ensure_dirs()
    return cfg


@pytest_asyncio.fixture
async def db(config):
    database = Database(config.db_path)
    await database.connect()
    yield database
    await database.close()
