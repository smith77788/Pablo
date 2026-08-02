"""«Хранилище» (Echo Vault) на ЖИВОМ Postgres: архивация business-переписки,
правки, удаления, чтение, поиск, шифрование at-rest и owner-скоуп.

Заглушка пула типы параметров не проверяет — а тут есть bind bigint[]/jsonb/
timestamptz и ON CONFLICT. Нужен живой драйвер. Без INFRAGRAM_TEST_DSN — skip
(см. docstring test_invite_e2e_postgres.py про поднятие Postgres за ~2 минуты).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
from types import SimpleNamespace as N

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 995001
OWNER2 = 995002
CONN = "conn_test_995001"

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _clean(pool):
    async def _c():
        await pool.execute("DELETE FROM vault_messages WHERE owner_id=ANY($1::bigint[])",
                           [OWNER, OWNER2])
        await pool.execute("DELETE FROM business_connections WHERE owner_id=ANY($1::bigint[])",
                           [OWNER, OWNER2])
    _run(_c())


def _chat(cid, name="Боб", uname="bob"):
    return N(id=cid, first_name=name, last_name=None, username=uname, title=None, full_name=name)


def _msg(chat_id, msg_id, from_id, text=None, **media):
    return N(chat=_chat(chat_id), message_id=msg_id, from_user=N(id=from_id),
             text=text, caption=None, date=None, edit_date=None,
             business_connection_id=CONN, **({} if not media else media))


# ── подключение ──────────────────────────────────────────────────────────────

def test_connection_upsert_and_lookup(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {"can_reply": True}))
    conn = _run(v.get_connection(pool, CONN))
    assert conn and conn["owner_id"] == OWNER and conn["can_reply"] is True
    # повторный upsert (напр. смена прав) не плодит дубли, обновляет
    _run(v.upsert_connection(pool, CONN, OWNER, 777, False, True, {}))
    conn = _run(v.get_connection(pool, CONN))
    assert conn["can_reply"] is False
    active = _run(v.active_connection_for_owner(pool, OWNER))
    assert active and active["connection_id"] == CONN


# ── архивация + шифрование ────────────────────────────────────────────────────

def test_archive_incoming_and_outgoing_encrypted(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    # входящее от собеседника (id=50), исходящее от владельца (id=OWNER)
    _run(v.archive_message(pool, _msg(50, 1, 50, "привет секрет"), OWNER, CONN))
    _run(v.archive_message(pool, _msg(50, 2, OWNER, "ответ владельца"), OWNER, CONN))

    msgs = _run(v.list_messages(pool, OWNER, 50))
    assert [m["direction"] for m in msgs] == ["in", "out"]
    assert msgs[0]["text"] == "привет секрет" and msgs[1]["text"] == "ответ владельца"

    # at-rest: в колонке text_enc лежит ШИФР (ENC:), а не открытый текст
    raw = _run(pool.fetchval(
        "SELECT text_enc FROM vault_messages WHERE owner_id=$1 AND chat_id=50 AND msg_id=1",
        OWNER))
    assert raw.startswith("ENC:") and "секрет" not in raw


def test_archive_is_idempotent(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    for _ in range(3):
        _run(v.archive_message(pool, _msg(50, 10, 50, "дубль"), OWNER, CONN))
    n = _run(pool.fetchval(
        "SELECT COUNT(*) FROM vault_messages WHERE owner_id=$1 AND chat_id=50 AND msg_id=10", OWNER))
    assert n == 1, "повторная доставка не должна плодить дубли"


def test_media_metadata_stored(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    photo = _msg(50, 20, 50, None, photo=[N(file_id="big", file_unique_id="u", file_size=999,
                                            mime_type=None, file_name=None)])
    _run(v.archive_message(pool, photo, OWNER, CONN))
    m = _run(v.list_messages(pool, OWNER, 50))[0]
    assert m["media_type"] == "photo" and m["media_label"] == "📷 Фото"


# ── правки и удаления ─────────────────────────────────────────────────────────

def test_edit_keeps_history(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    _run(v.archive_message(pool, _msg(50, 30, 50, "было"), OWNER, CONN))
    _run(v.record_edit(pool, _msg(50, 30, 50, "стало"), OWNER))
    m = _run(v.list_messages(pool, OWNER, 50))[0]
    assert m["text"] == "стало" and m["edited"] is True
    # прошлая версия сохранена (зашифрованной) в edit_history
    hist = _run(pool.fetchval(
        "SELECT edit_history FROM vault_messages WHERE owner_id=$1 AND chat_id=50 AND msg_id=30", OWNER))
    import json
    hist = hist if isinstance(hist, list) else json.loads(hist)
    assert len(hist) == 1 and hist[0].startswith("ENC:")


def test_delete_marks_but_keeps_content(pool):
    """Суть хранилища: удаление у пользователя НЕ стирает контент из архива."""
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    _run(v.archive_message(pool, _msg(50, 40, 50, "не потеряй меня"), OWNER, CONN))
    _run(v.archive_message(pool, _msg(50, 41, 50, "и меня"), OWNER, CONN))
    n = _run(v.mark_deleted(pool, OWNER, 50, [40, 41]))
    assert n == 2
    msgs = {m["msg_id"]: m for m in _run(v.list_messages(pool, OWNER, 50))}
    assert msgs[40]["deleted"] is True and msgs[40]["text"] == "не потеряй меня", \
        "контент удалённого сообщения обязан сохраниться"


# ── чтение / поиск / скоуп ────────────────────────────────────────────────────

def test_list_chats_and_search(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    _run(v.archive_message(pool, _msg(50, 1, 50, "яблоко"), OWNER, CONN))
    _run(v.archive_message(pool, _msg(60, 1, 60, "апельсин"), OWNER, CONN))
    chats = _run(v.list_chats(pool, OWNER))
    assert {c["chat_id"] for c in chats} == {50, 60}
    found = _run(v.search_messages(pool, OWNER, "яблок"))
    assert len(found) == 1 and found[0]["chat_id"] == 50


def test_owner_scope_isolation(pool):
    _clean(pool)
    from services import vault_service as v
    _run(v.upsert_connection(pool, CONN, OWNER, 777, True, True, {}))
    _run(v.archive_message(pool, _msg(50, 1, 50, "только мои"), OWNER, CONN))
    # другой владелец не видит чужой архив
    assert _run(v.list_chats(pool, OWNER2)) == []
    assert _run(v.list_messages(pool, OWNER2, 50)) == []
    assert _run(v.search_messages(pool, OWNER2, "мои")) == []
