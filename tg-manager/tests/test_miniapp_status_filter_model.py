"""Фильтр очереди в мини-аппе обязан знать все статусы модели состояний.

Разрыв. Список статусов, которые мини-апп принимает фильтром, был выписан в
`services/mini_app_api.py` руками. Модель состояний живёт отдельно
(`services/op_status.py`), и рукописный список от неё отставал — молча, потому
что ничто их не сверяло. Владелец видел это как отказ системы:

  * `partial` — у недоведённой работы появилось собственное терминальное
    состояние (операция взяла 203 цели из 380 и больше не врёт «✅ завершена»),
    но срез «частично выполненные» отдавал 400 «Неизвестный статус операции».
    Именно тот срез, ради которого статус и заводился, открыть было нельзя;
  * `waiting_approval` — этот статус ставит `database/db.py` при постановке
    операции, требующей подтверждения. Разобрать очередь по нему нельзя было с
    самого начала: операции ждут апрува, а найти их в списке нечем.

Чинится не дописыванием двух строк: список собирается из модели. Тогда
следующее состояние появится в фильтре вместе с собой, а не после жалобы.

Тест держит ОБА конца: и результат (статусы доезжают до SQL), и способ
(список — производная, а не копия). Без второго регресс вернётся при первой же
правке «просто добавлю статус сюда тоже».
"""
from __future__ import annotations

import inspect
import re

import pytest
from aiohttp import web

from services import mini_app_api, op_status


class _Pool:
    """Пул, запоминающий запросы: важно, доехал ли фильтр до SQL."""

    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        return []

    async def fetchrow(self, query: str, *args):
        self.queries.append(query)
        return None

    async def fetchval(self, query: str, *args):
        self.queries.append(query)
        return 0

    async def execute(self, query: str, *args):
        self.queries.append(query)
        return "UPDATE 0"


class _Req:
    def __init__(self, query=None):
        self.match_info: dict[str, str] = {}
        self.query = query or {}
        self.headers: dict[str, str] = {}

    async def json(self):
        return {}


def _operations_handler(pool):
    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter")
        if route.method == "GET" and path == "/api/miniapp/operations":
            return route.handler
    raise AssertionError("роут списка операций не найден")


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)


# ── Результат ────────────────────────────────────────────────────────────────

def test_whitelist_covers_the_whole_state_model():
    """Ни одно состояние модели не должно быть недоступно фильтром."""
    missing = (set(op_status.IN_FLIGHT) | set(op_status.TERMINAL)) - set(
        mini_app_api.OPERATION_STATUSES)
    assert not missing, (
        f"эти статусы очередь ставит, а отфильтровать по ним нельзя: {sorted(missing)}"
    )


@pytest.mark.parametrize("status", ["partial", "waiting_approval"])
def test_regressed_statuses_are_accepted(status):
    """Два конкретных среза, которые владелец не мог открыть."""
    assert status in mini_app_api.OPERATION_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(set(op_status.TERMINAL) | {"waiting_approval"}))
async def test_every_status_reaches_the_query(as_user, status):
    """Принять мало — фильтр обязан доехать до SQL, а не потеряться по пути.

    Молча снятый фильтр хуже отказа: человек видит полный список и считает,
    что операций в этом состоянии нет ни одной.
    """
    pool = _Pool()
    resp = await _operations_handler(pool)(_Req(query={"status": status}))
    assert resp.status == 200, f"{status}: отказ на существующем статусе"
    selects = [q for q in pool.queries if "FROM operation_queue" in q]
    assert selects and "oq.status=$2" in selects[-1], (
        f"{status}: фильтр не дошёл до запроса — вернулась вся очередь")


@pytest.mark.asyncio
async def test_garbage_is_still_rejected(as_user):
    """Расширение списка не должно превратиться в «принимаем что угодно»."""
    pool = _Pool()
    resp = await _operations_handler(pool)(_Req(query={"status": "running'; DROP"}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_unknown_but_plausible_status_is_rejected(as_user):
    """Опечатка в статусе — честная 400, а не тихо снятый фильтр."""
    pool = _Pool()
    resp = await _operations_handler(pool)(_Req(query={"status": "complete"}))
    assert resp.status == 400


# ── Способ ───────────────────────────────────────────────────────────────────

def test_whitelist_is_derived_not_copied():
    """Сторож самого фикса: копия снова отстанет от модели.

    Проверять тут нечего, кроме текста объявления: если список опять станет
    набором литералов, все проверки выше будут зелёными ровно до следующего
    нового состояния — то есть ровно до следующей жалобы владельца.
    """
    src = inspect.getsource(mini_app_api)
    decl = re.search(r"^OPERATION_STATUSES = (.+?)\n(?=\w|\n)", src, re.M | re.S)
    assert decl, "объявление списка статусов не найдено"
    body = decl.group(1)
    assert "op_status." in body, (
        "список статусов обязан собираться из модели состояний, а не выписываться"
    )
    assert not re.search(r"""['"](pending|running|done|failed|cancelled|partial)['"]""", body), (
        "в объявлении остались статусы-литералы — список снова копия модели"
    )


def test_no_hand_written_exceptions():
    """Вычитание из модели — тот же рукописный список, только наоборот.

    Исключение здесь означает «этот статус фильтровать нельзя», и проверить это
    утверждение неоткуда: оно снова отстанет. Пустой ответ на статус, которого в
    очереди не бывает, честнее отказа на статус, который в ней есть.
    """
    src = inspect.getsource(mini_app_api)
    decl = re.search(r"^OPERATION_STATUSES = (.+?)\n(?=\w|\n)", src, re.M | re.S)
    assert "-" not in decl.group(1), (
        "из набора статусов что-то вычитается — это рукописное исключение"
    )


def test_model_is_imported_once():
    """Дубль импорта — след автоправки; синтаксис его не ловит."""
    src = inspect.getsource(mini_app_api)
    assert src.count("from services import op_status\n") == 1
