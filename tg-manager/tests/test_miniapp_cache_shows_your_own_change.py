"""Человек не видел результата собственного действия: мешал кэш ответов.

Ответы `/api/miniapp/bots`, `/api/miniapp/channels` и дашборда кэшируются на 30
секунд (`_cached_user`). Сбрасывать его не умел никто: во всём файле не было ни
одного места, где кэш снимается после изменения данных.

Тридцати секунд хватало с избытком, потому что форма добавления бота стоит НА
экране ботов — значит список гарантированно уже в кэше к моменту, когда человек
вставляет токен:

    вставил токен → «✅ Бот @name добавлен!» → экран перезагрузил список →
    пришёл закэшированный ответ, снятый ДО добавления → бота в списке нет.

Вывод отсюда один, и он неверный: не получилось. Дальше человек вставляет токен
снова (там и ответ «уже добавлен» наготове) или уходит, решив, что продукт не
работает. То же на выключении бота — тост «убран из работы», а бот в списке
по-прежнему в строю — и на создании канала.

Здесь поднимается НАСТОЯЩИЙ сервер со всей цепочкой middleware и проверяется:
кэш работает (иначе проверка беспредметна), любое удачное изменение данных его
снимает, а неудачное — нет.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import ClientSession, web

from services import mini_app_api as M

UID = 604812
OTHER_UID = 604813
PORT = 18947


class _Pool:
    """Пул, считающий обращения: по ним видно, пришёл ответ из кэша или из базы."""

    def __init__(self):
        self.fetches = 0

    async def fetch(self, q, *a):
        self.fetches += 1
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        return "OK"


def _app(monkeypatch, pool, status: int = 200):
    """Роуты мини-аппа + тестовая мутация, чтобы не зависеть от требований
    конкретного эндпоинта (тариф, тело, внешние вызовы). Middleware — общие
    для всего приложения, значит проверяется ровно та цепочка, что в проде."""
    monkeypatch.setattr(M, "_get_uid", lambda request: UID)
    # Ограничитель частоты живёт в модуле и переживает тест: без сброса
    # несколько запросов с 127.0.0.1 в одном прогоне упираются в 429.
    try:
        from services import security as _sec

        _sec._rate_limiter._requests.clear()
    except Exception:
        pass
    M._cache.clear()

    app = web.Application()
    M.setup_routes(app, pool)

    async def _mutate(request):
        return web.json_response({"ok": True}, status=status)

    app.router.add_post("/api/test/mutate", _mutate)
    app.router.add_get("/api/test/read", _mutate)
    return app


async def _with_server(app, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    try:
        return await fn(f"http://127.0.0.1:{PORT}")
    finally:
        await runner.cleanup()


def test_the_cache_is_real(monkeypatch):
    """Страховка измерителя: без работающего кэша весь тест беспредметен."""
    pool = _Pool()

    async def go(base):
        async with ClientSession() as s:
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            first = pool.fetches
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            return first, pool.fetches

    first, second = asyncio.run(_with_server(_app(monkeypatch, pool), go))
    assert first > 0, "список ботов вообще не дошёл до базы — проверка измеряет не то"
    assert second == first, "ответ не кэшируется — этот тест защищает не тот механизм"


def test_a_change_makes_the_list_fresh_again(monkeypatch):
    """Добавил бота — и видит его в списке, а не ответ, снятый до добавления."""
    pool = _Pool()

    async def go(base):
        async with ClientSession() as s:
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            before = pool.fetches
            async with s.post(base + "/api/test/mutate") as r:
                assert r.status == 200, await r.text()
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            return before, pool.fetches

    before, after = asyncio.run(_with_server(_app(monkeypatch, pool), go))
    assert after > before, (
        "после изменения список снова пришёл из кэша: человек не видит "
        "результата собственного действия")


def test_a_refused_change_keeps_the_cache(monkeypatch):
    """Отказ ничего не изменил — сбрасывать кэш на каждом отказе незачем."""
    pool = _Pool()

    async def go(base):
        async with ClientSession() as s:
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            before = pool.fetches
            async with s.post(base + "/api/test/mutate") as r:
                await r.read()
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            return before, pool.fetches

    before, after = asyncio.run(_with_server(_app(monkeypatch, pool, status=403), go))
    assert after == before, "кэш снят из-за отказа, который ничего не менял"


def test_reading_does_not_drop_the_cache(monkeypatch):
    """Иначе кэша не существует: любой GET рядом снимал бы его."""
    pool = _Pool()

    async def go(base):
        async with ClientSession() as s:
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            before = pool.fetches
            async with s.get(base + "/api/test/read") as r:
                await r.read()
            async with s.get(base + "/api/miniapp/bots") as r:
                await r.read()
            return before, pool.fetches

    before, after = asyncio.run(_with_server(_app(monkeypatch, pool), go))
    assert after == before, "чтение сняло кэш — он перестал работать вовсе"


# ── Сам сброс ────────────────────────────────────────────────────────────────

def test_drop_takes_every_page_and_slice_of_that_user():
    """Ключ включает строку запроса: у одного списка записей столько, сколько
    страниц и срезов человек открыл. Снять надо все, иначе вторая страница
    останется старой."""
    M._cache.clear()
    for key in (f"bots:{UID}:limit=500&offset=0",
                f"bots:{UID}:limit=500&offset=500",
                f"channels:{UID}:search=новости",
                f"dashboard:{UID}:"):
        M._cache[key] = (1.0, ("snapshot",))
    M._cache[f"bots:{OTHER_UID}:limit=500&offset=0"] = (1.0, ("snapshot",))

    dropped = M._cache_drop_user(UID)
    assert dropped == 4, f"снято {dropped} записей вместо четырёх"
    assert f"bots:{OTHER_UID}:limit=500&offset=0" in M._cache, (
        "сброс задел чужие ответы — один человек чистил бы кэш всем")
    M._cache.clear()


def test_drop_does_not_match_a_user_id_inside_a_query_string():
    """Поиск подстроки ":uid:" задевал бы чужую запись, у которой такой же
    отрезок попал в параметры запроса."""
    M._cache.clear()
    trap = f"channels:{OTHER_UID}:search=a:{UID}:b"
    M._cache[trap] = (1.0, ("snapshot",))
    assert M._cache_drop_user(UID) == 0
    assert trap in M._cache, "сброс сработал по совпадению внутри строки запроса"
    M._cache.clear()


def test_drop_without_a_user_is_a_noop():
    """Неизвестный вызывающий не должен чистить кэш всему продукту."""
    M._cache.clear()
    M._cache[f"bots:{UID}:"] = (1.0, ("snapshot",))
    assert M._cache_drop_user(None) == 0
    assert M._cache, "сброс без пользователя снёс чужие ответы"
    M._cache.clear()


def test_invalidation_lives_in_one_place():
    """Разослать сброс по двум десяткам мутаций — значит забыть его в двадцать
    первой. Он обязан оставаться одним middleware."""
    src = Path(M.__file__).read_text("utf-8")
    assert src.count("async def cache_invalidate_middleware") == 1
    assert src.count("_cache_drop_user(") == 2, (
        "сброс кэша зовут не из одного места — вернулась россыпь по обработчикам")
