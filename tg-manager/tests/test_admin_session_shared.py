"""Сессионный доступ в админку — общий для процессов и с сроком годности.

Находка аудита №3. Вход по секретной фразе складывался в set уровня модуля
(bot.handlers.admin._session_admins), а от него зависит РЕШЕНИЕ О ДОСТУПЕ
(_is_admin в боте, мини-аппе и проверке тарифа). Значит:
  • вошёл в боте — в мини-аппе мог остаться не-админом (другой процесс);
  • доступ не истекал ВООБЩЕ: жил до перезапуска, то есть неделями;
  • перезапуск молча выкидывал всех вошедших.

Две «реплики» моделируются честно: два НЕЗАВИСИМЫХ экземпляра модуля со своей
памятью на одной БД. Заглушкой это не проверить — нужен настоящий Postgres.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _fresh_admin(tag: str):
    """Независимый экземпляр bot.handlers.admin — своя память, как отдельный процесс."""
    path = os.path.join(ROOT, "bot", "handlers", "admin.py")
    spec = importlib.util.spec_from_file_location(f"_adm_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        # Ровно та миграция, что уезжает в прод (комментарии идут до первой ';').
        with open(os.path.join(ROOT, "schema_v181.sql"), encoding="utf-8") as f:
            for chunk in f.read().split(";"):
                body = "\n".join(ln for ln in chunk.split("\n")
                                 if not ln.strip().startswith("--")).strip()
                if body:
                    await p.execute(body)
        await p.execute("DELETE FROM platform_admin_sessions")
        return p

    p = _run(_mk())
    yield p
    _run(p.close())


def test_login_in_one_process_is_visible_in_another(pool):
    """Вошёл в боте — обязан быть админом и в мини-аппе (другой процесс)."""
    a, b = _fresh_admin("a"), _fresh_admin("b")
    _run(pool.execute("DELETE FROM platform_admin_sessions"))
    a._session_admins.clear(); b._session_admins.clear()

    _run(a.grant_session_admin(pool, 12345))
    assert 12345 in a._session_admins                       # свой процесс — сразу
    assert 12345 not in b._session_admins                   # чужой — ещё не знает
    _run(b.refresh_session_admins(pool))
    assert 12345 in b._session_admins, (
        "вход не виден другому процессу — доступ зависит от того, кто обслужил запрос")


def test_session_has_expiry_and_expired_is_dropped(pool):
    a = _fresh_admin("exp")
    _run(pool.execute("DELETE FROM platform_admin_sessions"))
    _run(a.grant_session_admin(pool, 777))
    assert _run(a.refresh_session_admins(pool)) == 1

    # срок вышел — кэш обязан очиститься сам, без отдельной логики удаления
    _run(pool.execute(
        "UPDATE platform_admin_sessions SET expires_at = now() - interval '1 minute'"))
    _run(a.refresh_session_admins(pool))
    assert 777 not in a._session_admins, "истёкшая сессия осталась админской"


def test_access_survives_process_restart(pool):
    """Перезапуск больше не выкидывает вошедших: сессия в БД, а не в памяти."""
    a = _fresh_admin("r1")
    _run(pool.execute("DELETE FROM platform_admin_sessions"))
    _run(a.grant_session_admin(pool, 999))

    restarted = _fresh_admin("r2")          # «процесс перезапустился» — память пуста
    assert 999 not in restarted._session_admins
    _run(restarted.refresh_session_admins(pool))
    assert 999 in restarted._session_admins


def test_refresh_is_fail_open_on_db_error(pool):
    """Сбой БД не должен ВЫКИДЫВАТЬ уже вошедшего админа посреди работы."""
    a = _fresh_admin("fo")
    a._session_admins.clear(); a._session_admins.add(555)

    class _Broken:
        async def fetch(self, *_a, **_k):
            raise RuntimeError("БД недоступна")

    _run(a.refresh_session_admins(_Broken()))
    assert 555 in a._session_admins, "сбой обновления кэша разлогинил админа"


def test_grant_without_pool_still_works_locally():
    """Без пула (тесты/деградация) доступ хотя бы в своём процессе выдаётся."""
    a = _fresh_admin("nopool")
    a._session_admins.clear()
    _run(a.grant_session_admin(None, 42))
    assert 42 in a._session_admins


def test_ttl_is_configurable_and_bounded():
    a = _fresh_admin("ttl")
    assert a._ADMIN_SESSION_TTL_H >= 1
    saved = os.environ.get("ADMIN_SESSION_TTL_HOURS")
    os.environ["ADMIN_SESSION_TTL_HOURS"] = "12"
    try:
        b = _fresh_admin("ttl2")
        assert b._ADMIN_SESSION_TTL_H == 12
        os.environ["ADMIN_SESSION_TTL_HOURS"] = "мусор"
        c = _fresh_admin("ttl3")
        assert c._ADMIN_SESSION_TTL_H >= 1        # мусор не ломает старт
    finally:
        if saved is None:
            os.environ.pop("ADMIN_SESSION_TTL_HOURS", None)
        else:
            os.environ["ADMIN_SESSION_TTL_HOURS"] = saved
