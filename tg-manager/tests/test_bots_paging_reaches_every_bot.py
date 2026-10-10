"""Боты за потолком 5000 были недостижимы, а совет на экране — невыполним.

`/api/miniapp/bots` принимал только `limit`, без `offset`. Поэтому «загрузить
ещё» перезапрашивало срез побольше: 500, потом 1000, потом 1500 — те же строки
заново, трафиком в квадрате. И с потолком: `BOT_MAX = 5000`, дальше кнопка
просто не работала, а на её месте появлялась строка

    «Показано 5000 из 7400 — больше сервер за раз не отдаёт. Сузьте поиск.»

Выполнить этот совет было невозможно. Поиск по ботам шёл НА КЛИЕНТЕ, по тем
самым 5000 загруженным строкам, — сузив его, человек просеивал ровно тот набор,
который у него и так был, а остальные 2400 ботов не достигались никаким
действием в интерфейсе. Продукт, который создаёт ботов пачками, не давал
добраться до собственных.

Здесь стережётся: страница берётся по limit+offset, догрузка идёт со
следующего смещения и дополняет список, потолка нет, и невыполнимого совета
на экране тоже нет.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
UID = 770451


class _Pool:
    """Пул, запоминающий каждый запрос и его параметры."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return 7400

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, **query):
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        # Ответ кэшируется по uid И строке запроса: без query_string все вызовы
        # в тесте попали бы в один ключ, и второй получил бы ответ первого.
        self.query_string = urlencode(query)


def _handler(pool):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if route.method == "GET" and path == "/api/miniapp/bots":
            return route.handler
    raise AssertionError("роут /api/miniapp/bots не зарегистрирован")


def _call(pool, **query):
    return asyncio.run(_handler(pool)(_Req(**query)))


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    # Кэш ответов живёт в модуле и переживает тест: чистим, чтобы каждый
    # проверял свой запрос, а не повтор чужого.
    M._cache.clear()
    yield
    M._cache.clear()


def _page_query(pool) -> tuple[str, tuple]:
    for q, a in pool.calls:
        if "managed_bots" in q and "ORDER BY subscriber_count" in q:
            return q, a
    raise AssertionError("запроса страницы ботов не было")


def test_page_query_accepts_offset():
    pool = _Pool()
    _call(pool, limit="500", offset="1500")
    q, args = _page_query(pool)
    assert "OFFSET" in q.upper(), "страница ботов снова берётся без offset"
    assert 1500 in args, "смещение не доехало до запроса параметром"


def test_offset_is_echoed_back_so_the_client_can_page():
    pool = _Pool()
    d = _body(_call(pool, limit="500", offset="1000"))
    assert d["offset"] == 1000 and d["limit"] == 500, (
        "ответ не говорит, какую страницу отдал — клиенту нечем вести отсчёт")


def test_bad_offset_does_not_break_the_list():
    """Смещение приходит из адреса: мусор не должен ронять список ботов."""
    for bad in ("-5", "abc", "", "1e9999"):
        pool = _Pool()
        d = _body(_call(pool, offset=bad))
        assert d["offset"] >= 0, f"offset={bad!r} дал отрицательное смещение"


def test_visibility_condition_has_one_source():
    """Условие доступа к ботам лежало в файле ПЯТЬЮ копиями.

    Одна — в списке ботов и его счётчике, ещё четыре — дословно одинаковые
    проверки «мой ли это бот» в четырёх эндпоинтах. Разъедься любая из четырёх,
    и ровно этот эндпоинт начал бы отдавать чужого бота, пока остальные три
    отказывают правильно: снаружи такую дыру почти нечем заметить.
    """
    src = Path(M.__file__).read_text("utf-8")
    assert src.count("_BOTS_VISIBLE_SQL = ") == 1
    assert src.count("FROM ecosystem_bots eb") == 1, (
        "условие доступа к ботам снова размножено по файлу")
    assert src.count("async def _user_can_use_bot") == 1
    assert src.count("await _user_can_use_bot(pool") == 4, (
        "эндпоинт проверяет доступ к боту своей копией условия, а не общей")


def test_access_check_binds_the_bot_and_the_owner_in_the_right_slots():
    """Перепутанные местами параметры дали бы «доступ есть» на чужого бота."""
    seen: list[tuple[str, tuple]] = []

    class _P:
        async def fetchval(self, q, *a):
            seen.append((q, a))
            return 1

    ok = asyncio.run(M._user_can_use_bot(_P(), 555, 777))
    assert ok is True
    q, args = seen[0]
    assert args == (555, 777), f"порядок параметров сменился: {args}"
    assert "mb.bot_id=$2" in q, "бот больше не второй параметр — проверка смотрит не туда"
    assert "mb.added_by=$1" in q, "владелец больше не первый параметр"


def test_access_check_closes_on_a_database_error():
    """Отказ при сбое базы — единственный безопасный исход для проверки прав."""

    class _Broken:
        async def fetchval(self, q, *a):
            raise RuntimeError("база недоступна")

    assert asyncio.run(M._user_can_use_bot(_Broken(), 555, 777)) is False


def test_count_and_list_agree_on_the_same_column():
    """Счёт по строкам обещал бы страницы, которых нет."""
    src = Path(M.__file__).read_text("utf-8")
    i = src.index("async def bots(request")
    j = src.index("async def bot_detail(request")
    block = src[i:j]
    listed = re.search(r"SELECT DISTINCT\s+(\S+)", block)
    assert listed, "в списке ботов не найден SELECT DISTINCT — проверка ослепла"
    counted = re.findall(r"COUNT\(DISTINCT\s+([\w.]+)\)\s+FROM", block)
    assert counted, "рядом со списком не найден COUNT(DISTINCT …) — проверка ослепла"
    assert all(c == listed.group(1).rstrip(",") for c in counted), (
        f"список отдаёт DISTINCT {listed.group(1)}, а счётчик — {counted}: "
        "«загрузить ещё» будет предлагать несуществующие страницы")


# ── Мини-апп ─────────────────────────────────────────────────────────────────

def _fn(name: str) -> str:
    """Тело функции по балансу фигурных скобок."""
    src = INDEX.read_text("utf-8")
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


def test_load_more_asks_for_the_next_page():
    body = _fn("loadMoreBots")
    assert "offset=" in body, "догрузка ботов снова идёт без offset"
    assert "BOT_OFFSET" in body, "клиент не ведёт отсчёт загруженного"


def test_load_more_appends_instead_of_refetching_everything():
    body = _fn("loadMoreBots")
    assert "BOTS.push(" in body, (
        "догрузка снова заменяет весь список вместо добавления страницы")
    assert "BOT_LIMIT" not in body, (
        "вернулся рост limit — это перезапрос уже загруженных строк")


def test_load_more_survives_an_empty_page():
    """Иначе кнопка «загрузить ещё» вечная: ботов удалили, а счёт прежний."""
    body = _fn("loadMoreBots")
    assert "if (!more.length)" in body


def test_no_unreachable_ceiling():
    src = INDEX.read_text("utf-8")
    assert "BOT_MAX" not in src, (
        "вернулся потолок загрузки: боты за ним недостижимы никаким действием")


def test_screen_does_not_advise_the_impossible():
    """«Сузьте поиск» нельзя выполнить, пока поиск идёт по загруженному."""
    src = re.sub(r"^\s*//.*$", "", INDEX.read_text("utf-8"), flags=re.M)
    assert "больше сервер за раз не отдаёт" not in src, (
        "экран снова советует сузить поиск вместо того, чтобы отдать страницу")


def test_shared_cache_keeps_its_counters():
    """BOTS — общий кэш: наполнив его мимо счётчиков, следующая страница
    пришла бы с нулевого смещения и повторила первую."""
    body = _fn("openBotByUsername")
    assert "BOT_OFFSET" in body and "BOT_TOTAL" in body
