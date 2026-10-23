"""Test setup: config reads the environment at import time, so give it a dummy
bot and admins before any app module loads, and point the store at a fresh
SQLite file per test."""

import asyncio
import os
import sys

import pytest

os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMINS"] = "111,222"
os.environ["DATABASE_URL"] = ""
os.environ["VITALITY_PROVIDER_KEY"] = "test-provider-key"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402
from utils.eld import store  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.sqlite3"))
    asyncio.run(store.init_db())
    return store


def run(coro):
    return asyncio.run(coro)
