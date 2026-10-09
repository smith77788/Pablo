"""Регрессия: приостановленная операция управляема, а «пауза» не отменяет.

Что было сломано.

1. `pause_operations` переводит очередь `pending → paused`, но список операций
   принимал фильтром только pending/running/done/failed/cancelled. Запрос
   `?status=paused` молча отдавал ВСЮ очередь без фильтра — человек видел
   полный список и решал, что паузы не было. Чипа «Пауза» в интерфейсе тоже
   не было, а KPI-плитки считали только pending: после «Пауза» очередь на вид
   просто испарялась.

2. `cancel_operation` бил по `status IN ('pending','running')`, поэтому
   приостановленную операцию нельзя было ни отменить, ни снять поштучно —
   она висела, пока пользователь не возобновит всю очередь целиком.

3. На экране деталей кнопка «⏸ Приостановить» звала `cancelOp`: операция
   уходила в `cancelled` безвозвратно, хотя подпись обещала обратимое
   действие. Возобновить её было нечем — `resume` работает только с `paused`.

Теперь пауза и отмена — разные действия, у каждой операции есть свои
`pause`/`resume`, отмена спрашивает подтверждение.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index_html() -> str:
    return (Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text("utf-8")


# ── Фиктивный пул и запрос ───────────────────────────────────────────────────

class _Pool:
    """Пул с одной операцией: UPDATE ... RETURNING отдаёт строку только если
    текущий статус попадает в разрешённый набор.

    Набор берётся двумя способами, и оба обязательны. Отмена ушла за единую
    дверь `operation_bus.cancel`, и там разрешённые статусы передаются
    ПАРАМЕТРОМ (`b.status = ANY($3::text[])`), а не вписаны в текст запроса, —
    заглушка, которая умела только текст, считала набор пустым, отказывала в
    отмене и три теста падали на здоровом коде. Пауза и возобновление
    по-прежнему ходят своим UPDATE с литералом `AND status='pending'`, поэтому
    разбор текста остаётся.
    """

    def __init__(self, status: str = "pending", owner: int = 777):
        self.status = status
        self.owner = owner
        self.queries: list[str] = []

    def _allowed(self, query: str, args: tuple = ()) -> list[str]:
        m = re.search(r"status\s+IN\s*\(([^)]*)\)", query)
        if m:
            return re.findall(r"'([a-z_]+)'", m.group(1))
        m = re.search(r"status\s*=\s*'([a-z_]+)'\s*\n?\s*RETURNING", query)
        m2 = re.search(r"(?:AND|WHERE)\s+(?:\w+\.)?status\s*=\s*'([a-z_]+)'", query)
        if m2:
            return [m2.group(1)]
        if m:
            return [m.group(1)]
        # Статусы пришли параметром: ANY($N::text[]) — ищем список строк.
        for a in args:
            if isinstance(a, (list, tuple)) and a and all(isinstance(x, str) for x in a):
                return list(a)
        return []

    async def fetchrow(self, query: str, *args):
        self.queries.append(query)
        if "UPDATE operation_queue" not in query:
            return {"status": self.status}
        was = self.status
        if was in self._allowed(query, args):
            new = re.search(r"SET\s+status\s*=\s*'([a-z_]+)'", query)
            if new:
                self.status = new.group(1)
            # was_status/op_type/params читает operation_bus.cancel, когда
            # дописывает исход отменённой операции.
            return {"id": 1, "was_status": was, "op_type": "mass_invite",
                    "params": {}}
        return None

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        return []

    async def fetchval(self, query: str, *args):
        self.queries.append(query)
        return self.status

    async def execute(self, query: str, *args):
        self.queries.append(query)
        return "UPDATE 1"


class _Req:
    def __init__(self, match_info=None, query=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers: dict[str, str] = {}

    async def json(self):
        return {}


def _handler(pool, method: str, path: str):
    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == method and (info.get("path") or info.get("formatter")) == path:
            return route.handler
    raise AssertionError(f"роут не найден: {method} {path}")


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


# ── Список: фильтр по paused ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operations_filter_accepts_paused(as_user):
    """?status=paused должен доехать до SQL, а не быть тихо выброшенным."""
    pool = _Pool()
    handler = _handler(pool, "GET", "/api/miniapp/operations")
    resp = await handler(_Req(query={"status": "paused"}))
    assert resp.status == 200
    selects = [q for q in pool.queries if "FROM operation_queue" in q]
    assert selects and "oq.status=$2" in selects[-1], (
        "фильтр paused не дошёл до запроса — вернулась вся очередь")


@pytest.mark.asyncio
async def test_operations_rejects_unknown_status(as_user):
    """Неизвестный статус — честная 400, а не молча снятый фильтр."""
    pool = _Pool()
    handler = _handler(pool, "GET", "/api/miniapp/operations")
    resp = await handler(_Req(query={"status": "нет-такого"}))
    assert resp.status == 400


def test_paused_is_in_status_whitelist():
    assert "paused" in mini_app_api.OPERATION_STATUSES


# ── Отмена: paused тоже отменяется ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running", "paused"])
async def test_cancel_accepts_active_statuses(as_user, status):
    pool = _Pool(status=status)
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/cancel")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 200, f"нельзя отменить операцию в статусе {status}"
    assert pool.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_rejects_finished(as_user):
    pool = _Pool(status="done")
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/cancel")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 404


# ── Пауза и возобновление одной операции ─────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_single_operation(as_user):
    pool = _Pool(status="pending")
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/pause")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 200
    assert pool.status == "paused", "пауза обязана давать paused, а не cancelled"
    assert _body(resp)["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_running_remembers_the_intent(as_user):
    """Запущенную не рвём на полпути — но пауза больше и не теряется.

    Прежний контракт был «нельзя, только отменить» (409): владельцу
    предлагалось потерять уже сделанную работу, а само намерение нигде не
    оставалось — и когда текущий проход заканчивался сам (деплой, сброс
    зависшей, отсрочка по флуд-паузе), операция запускалась ЗАНОВО, хотя
    владелец нажал паузу.

    Инвариант прежний и проверяется здесь же: статус не меняется, прогон не
    прерывается. Новое — намерение записано, и поллер применит его на
    следующем входе операции в работу (schema_v249, pause_requested).
    """
    pool = _Pool(status="running")
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/pause")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 200
    assert pool.status == "running", (
        "идущую операцию рвать нельзя: пере-прогон продублировал бы действия")
    assert any("pause_requested=TRUE" in q for q in pool.queries), (
        "намерение не записано — пауза снова отпустится на первом же деплое")
    body = _body(resp)
    assert body.get("pause_requested") is True
    assert "паузу" in str(body.get("message", "")).lower(), (
        "владельцу надо сказать словами, что будет дальше")


@pytest.mark.asyncio
async def test_resume_single_operation(as_user):
    pool = _Pool(status="paused")
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/resume")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 200
    assert pool.status == "pending", "возобновление обязано вернуть в очередь"


@pytest.mark.asyncio
async def test_resume_non_paused_is_refused(as_user):
    pool = _Pool(status="done")
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/resume")
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/miniapp/operation/{op_id}/pause",
    "/api/miniapp/operation/{op_id}/resume",
])
async def test_single_op_controls_require_auth(monkeypatch, path):
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: None)
    handler = _handler(_Pool(), "POST", path)
    resp = await handler(_Req({"op_id": "1"}))
    assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/miniapp/operation/{op_id}/pause",
    "/api/miniapp/operation/{op_id}/resume",
])
async def test_single_op_controls_are_owner_scoped(as_user, path):
    """Без owner_id можно было бы управлять чужой операцией по её id."""
    pool = _Pool(status="pending" if path.endswith("pause") else "paused")
    handler = _handler(pool, "POST", path)
    await handler(_Req({"op_id": "1"}))
    updates = [q for q in pool.queries if "UPDATE operation_queue" in q]
    assert updates and "owner_id=$2" in updates[-1], "операция не скоупится по владельцу"


# ── Интерфейс ────────────────────────────────────────────────────────────────

def test_detail_pause_button_does_not_cancel():
    """Кнопка «Приостановить» обязана звать pauseOp, а не cancelOp."""
    html = _index_html()
    m = re.search(r"if \(o\.status==='pending'\) \{(.*?)\n    \} else if \(o\.status==='running'\)",
                  html, re.S)
    assert m, "блок управления операцией на экране деталей не найден"
    block = m.group(1)
    assert "pauseOp(" in block, "«Приостановить» должна звать pauseOp"
    assert "cancelOp(${o.id},true)" in block, "отмена обязана спрашивать подтверждение"
    assert "⏸ Приостановить" in block and "✕ Отменить" in block, (
        "пауза и отмена должны быть разными кнопками с разными подписями")


def test_pause_resume_helpers_call_right_endpoints():
    html = _index_html()
    assert "async function pauseOp(" in html and "async function resumeOp(" in html
    assert "'/api/miniapp/operation/'+id+'/pause'" in html
    assert "'/api/miniapp/operation/'+id+'/resume'" in html


def test_cancel_asks_confirmation_when_requested():
    html = _index_html()
    m = re.search(r"async function cancelOp\(id, confirm\) \{(.*?)\n\}", html, re.S)
    assert m, "cancelOp должна принимать флаг подтверждения"
    assert "askConfirm" in m.group(1), "отмена необратима — нужен вопрос пользователю"


def test_paused_filter_chip_and_kpi_present():
    html = _index_html()
    assert "filterOps('paused',this)" in html, "нет чипа фильтра «Пауза»"
    assert "o.status==='paused'" in html and "Пауза</div>" in html, (
        "KPI не показывает приостановленные — очередь выглядит исчезнувшей")


def test_paused_row_offers_resume_and_cancel():
    html = _index_html()
    assert "resumeOp(${o.id})" in html, "в списке нет кнопки «Возобновить» у paused"
    assert "o.status==='paused'" in html


def test_empty_state_uses_russian_status_label():
    """Пустое состояние показывало сырое «running» — интерфейс русский."""
    html = _index_html()
    assert "OPS_STATUS_RU(OPS_FILTER)" in html
    assert "const OPS_STATUS_LABELS" in html
    for raw in ("pending:'ожидают'", "running:'работают'", "paused:'пауза'"):
        assert raw in html, f"нет русской подписи статуса: {raw}"


def test_routes_registered():
    src = _api_src()
    for route in ("pause", "resume"):
        assert (f'app.router.add_post("/api/miniapp/operation/{{op_id}}/{route}", '
                f'{route}_operation)') in src, f"роут {route} не зарегистрирован"
