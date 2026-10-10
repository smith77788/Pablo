"""Content Memory — история постов на канал (va_channel_posts, schema_v223).

Проверяем запись тела поста, чтение окна истории (свежие сверху), обрезку и
клампинг лимита, fail-soft при сбое БД, регистрацию миграции и что путь
публикации (_exec_mass_publish) действительно пишет в историю.
"""
from __future__ import annotations

import os
import re

import pytest

from services import content_memory as cm
from tests.test_executors import FakePool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _CapPool(FakePool):
    """FakePool, запоминающий аргументы execute/fetch (базовый пишет только запрос)."""

    def __init__(self, fetch=None):
        super().__init__(fetch=fetch)
        self.exec_args = []
        self.fetch_args = []

    async def execute(self, q, *a):
        self.exec_args.append((q, a))
        return "INSERT 0 1"

    async def fetch(self, q, *a):
        self.fetch_args.append((q, a))
        return self._fetch


@pytest.mark.asyncio
async def test_record_published_inserts_owner_channel_body():
    pool = _CapPool()
    await cm.record_published(pool, 42, "-100123", "Привет, канал", op_id=7)
    assert len(pool.exec_args) == 1
    q, args = pool.exec_args[0]
    assert "INSERT INTO va_channel_posts" in q
    # owner_id, channel_key, op_id, pillar, body
    assert args[0] == 42
    assert args[1] == "-100123"
    assert args[2] == 7
    assert args[4] == "Привет, канал"


@pytest.mark.asyncio
async def test_record_published_truncates_long_body():
    pool = _CapPool()
    await cm.record_published(pool, 1, "@ch", "я" * 20000)
    _, args = pool.exec_args[0]
    assert len(args[4]) == cm._MAX_BODY


@pytest.mark.asyncio
async def test_recent_texts_returns_bodies():
    pool = FakePool(fetch=[{"body": "пост1"}, {"body": "пост2"}])
    got = await cm.recent_texts(pool, 42, "-100123", limit=5)
    assert got == ["пост1", "пост2"]


@pytest.mark.asyncio
async def test_recent_texts_skips_empty_bodies():
    pool = FakePool(fetch=[{"body": "a"}, {"body": ""}, {"body": None}])
    assert await cm.recent_texts(pool, 42, "@ch") == ["a"]


@pytest.mark.asyncio
async def test_recent_texts_clamps_limit_to_window():
    pool = _CapPool(fetch=[])
    await cm.recent_texts(pool, 1, "@ch", limit=10000)
    _, args = pool.fetch_args[0]
    assert args[2] == cm._MAX_WINDOW  # третий параметр — LIMIT


@pytest.mark.asyncio
async def test_record_failsoft_on_db_error():
    class Boom(FakePool):
        async def execute(self, *a, **k):
            raise RuntimeError("db down")
    # Не должно бросать — публикация не должна падать из-за истории.
    await cm.record_published(Boom(), 1, "@ch", "x")


@pytest.mark.asyncio
async def test_recent_texts_failsoft_empty_on_error():
    class Boom(FakePool):
        async def fetch(self, *a, **k):
            raise RuntimeError("db down")
    assert await cm.recent_texts(Boom(), 1, "@ch") == []


@pytest.mark.asyncio
async def test_recent_texts_for_owner_returns_bodies_across_channels():
    pool = _CapPool(fetch=[{"body": "a"}, {"body": "b"}])
    got = await cm.recent_texts_for_owner(pool, 42, limit=5)
    assert got == ["a", "b"]
    q, args = pool.fetch_args[0]
    assert "channel_key" not in q  # по всем каналам владельца
    assert args[0] == 42


@pytest.mark.asyncio
async def test_recent_texts_for_owner_failsoft():
    class Boom(FakePool):
        async def fetch(self, *a, **k):
            raise RuntimeError("db down")
    assert await cm.recent_texts_for_owner(Boom(), 1) == []


def test_migration_registered_in_checksums():
    fname = "schema_v223_va_channel_posts.sql"
    assert os.path.exists(os.path.join(ROOT, fname)), "миграция отсутствует"
    manifest = open(os.path.join(ROOT, "schema_checksums.txt"), encoding="utf-8").read()
    assert fname in manifest, "миграция не внесена в schema_checksums.txt"


def test_mass_publish_records_into_content_memory():
    """Путь публикации ДОЛЖЕН наполнять историю — иначе антиповтор останется пустым.

    Проверяем по исходнику op_worker: вызов content_memory.record_published стоит
    в _exec_mass_publish. Если wiring уберут — тест падёт, и «источник recent_texts»
    снова станет фикцией.
    """
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    idx = src.find("async def _exec_mass_publish(")
    assert idx > 0, "_exec_mass_publish не найдена"
    body = src[idx:]
    # ограничиваемся телом этой функции (до начала следующей top-level async def)
    nxt = re.search(r"\nasync def ", body[1:])
    if nxt:
        body = body[: nxt.start() + 1]
    assert "content_memory.record_published(" in body, (
        "публикация не пишет в content_memory — recent_texts брать неоткуда"
    )
