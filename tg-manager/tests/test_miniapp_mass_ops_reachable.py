"""Регрессия: массовые операции Mini App доходят до очереди, а не падают в 500.

Что было сломано.

`from services import operation_bus as _obus` стоял ВНУТРИ веток
`if op == "check":` / `if op == "scan":` в `accounts_mass` и внутри
`if op == "promote":` в `channels_mass`. Любое связывание имени в теле функции
делает это имя локальным на всю функцию, поэтому на остальных ветках
(`leave_all`, профильные операции аккаунтов, массовое редактирование каналов)
обращение к `_obus` давало `UnboundLocalError`, хотя модульный импорт был на
месте. Ошибка — подкласс `NameError`, её ловил общий `except Exception`, и
пользователь получал 500 «Failed to …» без причины. То же самое в
`retry_operation`: `import json as _json` жил в ветке `mass_publish`, а
использовался при повторе ЛЮБОЙ другой операции.

Тест поднимает роутер с фиктивным пулом и дёргает ровно эти ветки: важно не то,
что операция выполнится, а что запрос доходит до постановки в очередь и не
возвращает 500.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web


# ── Фикстуры окружения ───────────────────────────────────────────────────────

class _FakePool:
    """Пул, отвечающий заранее заданными строками. Достаточно для веток,
    которые нас интересуют: резолв владения + постановка в очередь."""

    def __init__(self, rows: list[dict] | None = None, row: dict | None = None):
        self._rows = rows or []
        self._row = row
        self.executed: list[str] = []

    async def fetch(self, query: str, *args) -> list[dict]:
        return list(self._rows)

    async def fetchrow(self, query: str, *args) -> dict | None:
        return self._row

    async def fetchval(self, query: str, *args) -> Any:
        return None

    async def execute(self, query: str, *args) -> str:
        self.executed.append(query)
        return "UPDATE 1"


class _Req:
    """Минимальный aiohttp-подобный запрос."""

    def __init__(self, body: dict | None = None, match_info: dict | None = None):
        self._body = body or {}
        self.match_info = match_info or {}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    async def json(self) -> dict:
        return self._body


def _handler(pool, method: str, path: str):
    """Собирает роутер на фиктивном пуле и достаёт нужный обработчик."""
    from services import mini_app_api

    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        pattern = info.get("path") or info.get("formatter")
        if route.method == method and pattern == path:
            return route.handler
    raise AssertionError(f"роут не найден: {method} {path}")


@pytest.fixture
def submitted(monkeypatch):
    """Подменяет постановку в очередь и копит вызовы."""
    from services import operation_bus

    calls: list[dict] = []

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        calls.append({"op_type": op_type, "params": params, **kw})
        return 4242

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    return calls


@pytest.fixture
def as_user(monkeypatch):
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    return 777


def _payload(response: web.Response) -> dict:
    assert response.status != 500, f"эндпоинт вернул 500: {response.body!r}"
    return json.loads(response.body.decode("utf-8"))


# ── accounts_mass: ветки без локального import'а ─────────────────────────────

def _accounts_pool():
    return _FakePool(rows=[{"id": 11}, {"id": 12}])


@pytest.mark.asyncio
async def test_accounts_mass_leave_all_enqueues(as_user, submitted):
    """`leave_all` использовал _obus, связанный только в ветке `check`."""
    pool = _accounts_pool()
    handler = _handler(pool, "POST", "/api/miniapp/accounts/mass")
    resp = await handler(_Req({"op": "leave_all", "account_ids": [11, 12]}))
    data = _payload(resp)
    assert data.get("ok") is True, data
    assert [c["op_type"] for c in submitted] == ["leave_all_chats"] * 2
    assert data["count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("op, extra", [
    ("clear_bio", {}),
    ("remove_username", {}),
    ("set_online", {}),
    ("check_restriction", {}),
])
async def test_accounts_mass_profile_ops_enqueue(as_user, submitted, op, extra):
    """Профильные массовые операции — та же ветка без локального import'а."""
    pool = _accounts_pool()
    handler = _handler(pool, "POST", "/api/miniapp/accounts/mass")
    resp = await handler(_Req({"op": op, "account_ids": [11, 12], **extra}))
    data = _payload(resp)
    assert data.get("ok") is True, data
    assert [c["op_type"] for c in submitted] == ["profile_setter"]


@pytest.mark.asyncio
async def test_accounts_mass_check_still_works(as_user, submitted):
    """Ветка, которая работала и раньше, не сломана правкой."""
    pool = _accounts_pool()
    handler = _handler(pool, "POST", "/api/miniapp/accounts/mass")
    resp = await handler(_Req({"op": "check", "account_ids": [11, 12]}))
    data = _payload(resp)
    assert data.get("ok") is True and data["op_id"] == 4242
    assert submitted[0]["op_type"] == "check_accounts_health"


# ── channels_mass: массовое редактирование каналов ───────────────────────────

def _channels_pool():
    return _FakePool(rows=[
        {"channel_id": -100_1, "title": "A", "acc_id": 5,
         "access_hash": 1, "username": "a"},
        {"channel_id": -100_2, "title": "B", "acc_id": 6,
         "access_hash": 2, "username": "b"},
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["title", "about", "username"])
async def test_channels_mass_edit_enqueues(as_user, submitted, op):
    """Редактирование каналов использовало _obus из ветки `promote`."""
    pool = _channels_pool()
    handler = _handler(pool, "POST", "/api/miniapp/channels/mass")
    resp = await handler(_Req({"op": op, "channel_ids": [-1001, -1002],
                               "value": "Новое значение"}))
    data = _payload(resp)
    assert data.get("ok") is True, data
    assert submitted and submitted[0]["op_type"] == "bulk_chan_exec"
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_channels_mass_title_length_validated(as_user, submitted):
    """Валидация длины названия срабатывает раньше очереди."""
    pool = _channels_pool()
    handler = _handler(pool, "POST", "/api/miniapp/channels/mass")
    resp = await handler(_Req({"op": "title", "channel_ids": [-1001],
                               "value": "x" * 129}))
    assert resp.status == 400
    assert not submitted


# ── retry_operation: повтор НЕ mass_publish ──────────────────────────────────

@pytest.mark.asyncio
async def test_retry_non_mass_publish_operation(as_user, submitted):
    """params приходит из asyncpg строкой — разбор требовал `_json`,
    связанного только в ветке mass_publish."""
    pool = _FakePool(row={
        "op_type": "check_accounts_health",
        "params": json.dumps({"account_ids": [11, 12]}),   # jsonb → str
        "label": "Проверка 2 аккаунтов",
        "status": "done",
        "total_items": 2,
        "done_items": 1,
        "err_cnt": 1,
    })
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/retry")
    resp = await handler(_Req({}, {"op_id": "99"}))
    data = _payload(resp)
    assert data.get("ok") is True, data
    assert data["new_id"] == 4242
    assert submitted[0]["op_type"] == "check_accounts_health"
    # params должны доехать разобранными, а не строкой
    _p = submitted[0]["params"]
    assert _p["account_ids"] == [11, 12]
    # ...и нести ссылку на исходную операцию: по ней исполнитель видит её журнал
    # и не делает второй раз уже сделанное (op_worker.journal_op_ids).
    assert _p["retry_of_op"] == 99
    assert set(_p) == {"account_ids", "retry_of_op"}, "в повтор просочились лишние параметры"
