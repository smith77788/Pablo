"""Автобэкап БД: снятие дампа, нарезка на части, точное восстановление.

Роундтрип (create → parse → restore) гоняем в ОТДЕЛЬНОЙ временной базе, чтобы не
трогать данные основного тестового кластера. Плюс чистые функции (split/parse) и
разводка (цикл в main.py, команда /backup).
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import db_backup as bk  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Чистые функции ─────────────────────────────────────────────────────────────
def test_split_parts_small_and_large():
    assert bk.split_parts(b"x" * 10, part_size=100) == [b"x" * 10]
    parts = bk.split_parts(b"abcdefg", part_size=3)
    assert parts == [b"abc", b"def", b"g"]
    assert b"".join(parts) == b"abcdefg"


def test_backup_chat_id_from_env(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_CHAT_ID", "12345")
    assert bk.backup_chat_id() == 12345
    monkeypatch.setenv("DB_BACKUP_CHAT_ID", "не-число")
    # падает на ADMIN_IDS (в тестах пусто) → None, но не бросает
    assert bk.backup_chat_id() is None or isinstance(bk.backup_chat_id(), int)


def test_excluded_tables_default_and_override(monkeypatch):
    monkeypatch.delenv("DB_BACKUP_EXCLUDE", raising=False)
    assert "activity_log" in bk._excluded_tables()
    monkeypatch.setenv("DB_BACKUP_EXCLUDE", "a, b ,c")
    assert bk._excluded_tables() == {"a", "b", "c"}


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_ENABLED", "0")
    assert bk._enabled() is False
    monkeypatch.setenv("DB_BACKUP_ENABLED", "1")
    assert bk._enabled() is True


# ── Разводка ───────────────────────────────────────────────────────────────────
def test_wired_into_main_and_command():
    m = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "db_backup.run_backup_loop" in m, "цикл автобэкапа не запущен"
    a = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert 'Command("backup")' in a, "нет команды /backup"


class _FakeBot:
    def __init__(self, fail=False):
        self.docs = []
        self.fail = fail
    async def send_document(self, chat_id, document, caption=None):
        if self.fail:
            raise RuntimeError("сеть Telegram упала")
        self.docs.append((chat_id, getattr(document, "filename", "?"), caption))


def _acoro(v):
    async def _c(*a, **k):
        return v
    return _c()


# ── run_backup_once: пути ошибок (fail-soft) ───────────────────────────────────
def test_run_backup_once_no_destination(monkeypatch):
    monkeypatch.setattr(bk, "backup_chat_id", lambda: None)
    res = _run(bk.run_backup_once(object(), _FakeBot()))
    assert res["ok"] is False and "назначени" in res["error"]


def test_run_backup_once_dump_failure(monkeypatch):
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 5)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro_raise("дамп упал"))
    res = _run(bk.run_backup_once(object(), _FakeBot()))
    assert res["ok"] is False and "дамп" in res["error"]


def test_run_backup_once_send_failure(monkeypatch):
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 5)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro(b"arch"))
    res = _run(bk.run_backup_once(object(), _FakeBot(fail=True)))
    assert res["ok"] is False and "отправка" in res["error"] and res["size"] == 4


def test_run_backup_once_ok(monkeypatch):
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 42)
    monkeypatch.setattr(bk, "create_archive", lambda pool, **k: _acoro(b"hello"))
    bot = _FakeBot()
    res = _run(bk.run_backup_once(object(), bot))
    assert res["ok"] is True and res["parts"] == 1 and bot.docs[0][0] == 42


def test_send_backup_splits_into_parts(monkeypatch):
    monkeypatch.setattr(bk, "_PART_LIMIT", 3)
    monkeypatch.setattr(bk.asyncio, "sleep", lambda *a, **k: _acoro(None))
    bot = _FakeBot()
    n = _run(bk.send_backup(bot, 7, b"abcdefg", caption_note="важно"))
    assert n == 3 and len(bot.docs) == 3
    # первая часть несёт подпись с числом частей и заметкой
    assert "частей: 3" in bot.docs[0][2] and "важно" in bot.docs[0][2]
    assert bot.docs[1][2] is None            # остальные — без подписи
    assert "part002of003" in bot.docs[1][1]


def test_loop_disabled_returns(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_ENABLED", "0")
    assert _run(bk.run_backup_loop(object(), _FakeBot())) is None


def test_loop_no_destination_returns(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_ENABLED", "1")
    monkeypatch.setattr(bk, "backup_chat_id", lambda: None)
    assert _run(bk.run_backup_loop(object(), _FakeBot())) is None


def _acoro_raise(msg):
    async def _c(*a, **k):
        raise RuntimeError(msg)
    return _c()


def test_backup_chat_id_admin_fallback(monkeypatch):
    monkeypatch.delenv("DB_BACKUP_CHAT_ID", raising=False)
    import config
    monkeypatch.setattr(config, "ADMIN_IDS", [7788, 999], raising=False)
    assert bk.backup_chat_id() == 7788


def test_loop_runs_one_iteration_then_cancels(monkeypatch):
    monkeypatch.setenv("DB_BACKUP_ENABLED", "1")
    monkeypatch.setattr(bk, "backup_chat_id", lambda: 5)
    ran = []
    monkeypatch.setattr(bk, "run_backup_once",
                        lambda pool, bot, note="": _record(ran, note))
    calls = {"n": 0}

    async def sleeper(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:           # старт (120с) — 1-я; после тика — 2-я
            raise asyncio.CancelledError()

    monkeypatch.setattr(bk.asyncio, "sleep", sleeper)
    with pytest.raises(asyncio.CancelledError):
        _run(bk.run_backup_loop(object(), _FakeBot()))
    assert ran == ["автоматический"]


def _record(bucket, item):
    async def _c():
        bucket.append(item)
        return {"ok": True}
    return _c()


# ── Роундтрип в изолированной базе ─────────────────────────────────────────────
@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_backup_restore_roundtrip_isolated():
    import asyncpg

    async def _r():
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS bkp_rt WITH (FORCE)")
            await admin.execute("CREATE DATABASE bkp_rt")
        finally:
            await admin.close()

        # DSN на новую базу
        import re
        target = re.sub(r"/[^/?]+(\?|$)", r"/bkp_rt\1", DSN, count=1)
        pool = await asyncpg.create_pool(target, min_size=1, max_size=3)
        try:
            async with pool.acquire() as c:
                await c.execute(
                    "CREATE TABLE parent(id serial PRIMARY KEY, name text)")
                await c.execute(
                    "CREATE TABLE child(id serial PRIMARY KEY, "
                    "parent_id int REFERENCES parent(id), note text)")
                await c.execute("INSERT INTO parent(name) VALUES('Алиса'),('Боб'),('Ева')")
                await c.execute(
                    "INSERT INTO child(parent_id, note) VALUES(1,'a'),(1,'b'),(3,'c')")

            # снять дамп (ничего не исключаем)
            archive = await bk.create_archive(pool, exclude=set())
            assert isinstance(archive, bytes) and len(archive) > 0

            manifest, tables = bk.parse_archive(archive)
            assert set(tables) >= {"parent", "child"}
            assert manifest["row_counts"]["parent"] == 3
            assert "parent_id_seq" in manifest.get("sequences", {}) or \
                   "parent_id_seq" in " ".join(manifest.get("sequences", {}).keys())

            # испортить данные
            async with pool.acquire() as c:
                await c.execute("TRUNCATE child, parent RESTART IDENTITY CASCADE")
                assert await c.fetchval("SELECT count(*) FROM parent") == 0

            # восстановить
            report = await bk.restore_archive(pool, archive)
            assert "parent" in report["restored"] and "child" in report["restored"]

            async with pool.acquire() as c:
                assert await c.fetchval("SELECT count(*) FROM parent") == 3
                assert await c.fetchval("SELECT count(*) FROM child") == 3
                names = await c.fetch("SELECT name FROM parent ORDER BY id")
                assert [r["name"] for r in names] == ["Алиса", "Боб", "Ева"]
                # секвенция восстановлена: новый insert не конфликтует по PK
                await c.execute("INSERT INTO parent(name) VALUES('Новый')")
                assert await c.fetchval("SELECT count(*) FROM parent") == 4
        finally:
            await pool.close()
            admin = await asyncpg.connect(DSN)
            try:
                await admin.execute("DROP DATABASE IF EXISTS bkp_rt WITH (FORCE)")
            finally:
                await admin.close()

    _run(_r())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_run_backup_once_sends_via_fake_bot():
    import asyncpg

    class _FakeBot:
        def __init__(self):
            self.docs = []
        async def send_document(self, chat_id, document, caption=None):
            self.docs.append((chat_id, getattr(document, "filename", "?"), caption))

    async def _r(monkeyenv):
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS bkp_send WITH (FORCE)")
            await admin.execute("CREATE DATABASE bkp_send")
        finally:
            await admin.close()
        import re
        target = re.sub(r"/[^/?]+(\?|$)", r"/bkp_send\1", DSN, count=1)
        pool = await asyncpg.create_pool(target, min_size=1, max_size=2)
        bot = _FakeBot()
        try:
            async with pool.acquire() as c:
                await c.execute("CREATE TABLE t(id int)")
                await c.execute("INSERT INTO t VALUES (1),(2)")
            res = await bk.run_backup_once(pool, bot, note="тест")
            assert res["ok"] is True and res["parts"] == 1
            assert bot.docs and bot.docs[0][0] == 999  # ушло в заданный чат
            assert bot.docs[0][1].endswith(".tar.gz")
        finally:
            await pool.close()
            admin = await asyncpg.connect(DSN)
            try:
                await admin.execute("DROP DATABASE IF EXISTS bkp_send WITH (FORCE)")
            finally:
                await admin.close()

    # назначение бэкапа — фиксированный чат
    os.environ["DB_BACKUP_CHAT_ID"] = "999"
    try:
        _run(_r(None))
    finally:
        os.environ.pop("DB_BACKUP_CHAT_ID", None)
