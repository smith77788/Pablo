"""Хранилище «Модератора чатов» по живому Postgres (register/settings/warns)."""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 993401
CHAT = -1001234567890
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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _clean(pool):
    _run(pool.execute("DELETE FROM guard_chats WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM guard_warnings WHERE chat_id=$1", CHAT))


def test_register_activate_and_defaults(pool):
    from services import chat_guard as cg
    _clean(pool)
    rec = _run(cg.register_chat(pool, OWNER, CHAT, title="Мой чат", username="mychat"))
    assert rec["is_active"] is True
    assert rec["settings"]["clean_service"] is True   # дефолты влиты
    # is_guarded возвращает активный чат
    g = _run(cg.is_guarded(pool, CHAT))
    assert g and g["chat_id"] == CHAT
    # список для оператора
    lst = _run(cg.list_chats(pool, OWNER))
    assert any(c["chat_id"] == CHAT for c in lst)


def test_reregister_preserves_settings(pool):
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT, title="T"))
    _run(cg.set_setting(pool, CHAT, "antispam_links", True))
    _run(cg.deactivate_chat(pool, CHAT))
    assert _run(cg.is_guarded(pool, CHAT)) is None      # снят с охраны
    # повторная выдача админки: реактивирует, НЕ сбрасывая настройку
    rec = _run(cg.register_chat(pool, OWNER, CHAT, title="T2"))
    assert rec["is_active"] is True
    assert rec["settings"]["antispam_links"] is True
    assert rec["title"] == "T2"


def test_toggle_and_set_setting(pool):
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    s = _run(cg.toggle_setting(pool, CHAT, "clean_join"))
    assert s["clean_join"] is False           # был True → инвертирован
    s = _run(cg.toggle_setting(pool, CHAT, "clean_join"))
    assert s["clean_join"] is True            # обратно
    s = _run(cg.set_setting(pool, CHAT, "welcome_text", "Привет, {name}!"))
    assert s["welcome_text"] == "Привет, {name}!"
    # неизвестный ключ — ошибка
    with pytest.raises(ValueError):
        _run(cg.set_setting(pool, CHAT, "nope", 1))


def test_warnings_flow(pool):
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    assert _run(cg.get_warnings(pool, CHAT, 555)) == 0
    assert _run(cg.add_warning(pool, CHAT, 555)) == 1
    assert _run(cg.add_warning(pool, CHAT, 555)) == 2
    assert _run(cg.get_warnings(pool, CHAT, 555)) == 2
    _run(cg.reset_warnings(pool, CHAT, 555))
    assert _run(cg.get_warnings(pool, CHAT, 555)) == 0
    # счётчики раздельны по пользователям
    _run(cg.add_warning(pool, CHAT, 777))
    assert _run(cg.get_warnings(pool, CHAT, 777)) == 1
    assert _run(cg.get_warnings(pool, CHAT, 555)) == 0
