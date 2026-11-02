"""Бэкап проверяется восстановлением, копия уходит в S3, данные владельца
выгружаются и удаляются по его запросу.

Postgres-части пропускаются без INFRAGRAM_TEST_DSN (в CI база есть).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import zipfile
from datetime import datetime

import pytest

from services import db_backup as bk

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
needs_pg = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── S3: подпись совпадает с эталоном botocore ───────────────────────────────
def test_s3_signature_matches_botocore_reference():
    """Эталон снят с botocore.auth.S3SigV4Auth на тех же данных (2026-11-02)."""
    cfg = {"endpoint": "https://s3.example.com", "bucket": "my-bucket",
           "access": "AKIDEXAMPLE",
           "secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "region": "us-east-1"}
    url, h = bk.s3_sign_put(cfg, "infragram/a.tar.gz.enc", b"hello backup",
                            datetime(2026, 11, 2, 6, 32, 2))
    assert url == "https://s3.example.com/my-bucket/infragram/a.tar.gz.enc"
    assert h["Authorization"].endswith(
        "Signature=e0fd9f544f1c8323dd5c7afcc8afa2477522109567a1a1cf1b1cd775d521072d")
    assert h["x-amz-date"] == "20261102T063202Z"


def test_s3_is_off_without_config(monkeypatch):
    for k in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY",
              "BACKUP_S3_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert _run(bk.upload_to_s3(b"x", "f")) is None


def test_s3_failure_does_not_cancel_telegram_copy(monkeypatch):
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = b"id\n1\n"
        info = tarfile.TarInfo("data/t.csv")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    arch = buf.getvalue()

    async def _arch(pool, **k):
        return arch

    async def _boom(payload, name):
        raise RuntimeError("S3 ответил 403")

    sent = []

    class _Bot:
        async def send_document(self, chat_id, document, caption=None, **kw):
            sent.append(caption)

    monkeypatch.setattr(bk, "backup_chat_id", lambda: 1)
    monkeypatch.setattr(bk, "create_archive", _arch)
    monkeypatch.setattr(bk, "upload_to_s3", _boom)
    res = _run(bk.run_backup_once(object(), _Bot()))
    assert res["ok"] is True and "403" in res["s3_error"] and sent


def test_csv_row_count_respects_quoted_newlines():
    assert bk._csv_rows(b'id,note\n1,"a\nb"\n2,c\n') == 2


# ── Учение восстановления ───────────────────────────────────────────────────
def _db(name):
    return re.sub(r"/[^/?]+(\?|$)", rf"/{name}\1", DSN, count=1)


async def _fresh(name):
    import asyncpg
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {name}")
    finally:
        await admin.close()
    return await asyncpg.create_pool(_db(name), min_size=1, max_size=2)


async def _drop(name):
    import asyncpg
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    finally:
        await admin.close()


@needs_pg
def test_restore_drill_restores_and_leaves_no_trace():
    async def go():
        pool = await _fresh("bkp_drill")
        try:
            async with pool.acquire() as c:
                await c.execute("CREATE TABLE notes(id serial PRIMARY KEY, body text)")
                await c.execute("INSERT INTO notes(body) VALUES ('a'), (E'мульти\\nстрока'), ('c')")
            arch = await bk.create_archive(pool, exclude=set())
            rep = await bk.restore_drill(pool, arch)
            assert rep["ok"] is True and rep["tables"] >= 1 and rep["rows"] >= 3
            async with pool.acquire() as c:
                assert await c.fetchval(
                    "SELECT count(*) FROM pg_namespace WHERE nspname='restore_drill_tmp'") == 0
                assert await c.fetchval("SELECT count(*) FROM notes") == 3
        finally:
            await pool.close()
            await _drop("bkp_drill")
    _run(go())


# ── Данные владельца ────────────────────────────────────────────────────────
@needs_pg
def test_owner_export_and_delete():
    from services import owner_data, token_vault

    async def go():
        pool = await _fresh("owner_data_t")
        try:
            async with pool.acquire() as c:
                await c.execute("""CREATE TABLE platform_users(user_id bigint PRIMARY KEY,
                                   username text, first_name text, notes text)""")
                await c.execute("CREATE TABLE payments(id serial, owner_id bigint, amount int)")
                await c.execute("""CREATE TABLE channels(id serial PRIMARY KEY,
                                   owner_id bigint, title text)""")
                await c.execute("""CREATE TABLE posts(id serial, owner_id bigint,
                                   channel_id int REFERENCES channels(id), body text)""")
                await c.execute("CREATE TABLE tg_accounts(id serial, owner_id bigint, session_str text)")
                await c.execute("INSERT INTO platform_users VALUES (1,'own','Имя','n'),(2,'x','Y',null)")
                await c.execute("INSERT INTO payments(owner_id, amount) VALUES (1, 10)")
                await c.execute("INSERT INTO channels(owner_id, title) VALUES (1,'мой'),(2,'чужой')")
                await c.execute("INSERT INTO posts(owner_id, channel_id, body) VALUES (1,1,'p')")
                await c.execute("INSERT INTO tg_accounts(owner_id, session_str) VALUES (1,$1)",
                                token_vault.encrypt_token("1AgAOMTQ5LjE1NC4xNjcuNTAB"))

            blob, counts = await owner_data.export_owner_data(pool, 1)
            assert counts["channels"] == 1 and counts["platform_users"] == 1
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                acc = json.loads(zf.read("tg_accounts.json"))
                assert acc[0]["session_str"] == "<зашифровано>"
                assert "чужой" not in zf.read("channels.json").decode()

            res = await owner_data.delete_owner_data(pool, 1)
            assert res["deleted"]["channels"] == 1 and res["deleted"]["posts"] == 1
            async with pool.acquire() as c:
                assert await c.fetchval("SELECT count(*) FROM channels WHERE owner_id=1") == 0
                assert await c.fetchval("SELECT count(*) FROM channels WHERE owner_id=2") == 1
                assert await c.fetchval("SELECT count(*) FROM payments WHERE owner_id=1") == 1
                row = await c.fetchrow("SELECT username, first_name FROM platform_users WHERE user_id=1")
                assert row["username"] is None and row["first_name"] is None
        finally:
            await pool.close()
            await _drop("owner_data_t")
    _run(go())


def test_my_data_commands_are_wired_and_not_gated():
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "dp.include_router(my_data_handler.router)" in main
    gate = open(os.path.join(ROOT, "bot", "middlewares", "subscription_gate.py"),
                encoding="utf-8").read()
    assert '["/delete_my_data"]' in gate
    h = open(os.path.join(ROOT, "bot", "handlers", "my_data.py"), encoding="utf-8").read()
    assert "CONFIRM_WORD" in h and 'F.chat.type == "private"' in h
