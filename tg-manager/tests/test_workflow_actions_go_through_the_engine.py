"""Действия со сценарием идут через движок, а шаг проверяется одним словарём.

ЧТО ЛОМАЛОСЬ. На каждое действие со сценарием в проекте жили ДВЕ реализации:
аккуратная в `services/workflow_engine.py` и переписанная заново рядом с
маршрутом в `mini_app_api.py`. Кнопка в мини-аппе нажимала наивную.

  * Удаление. Экран зовёт `DELETE /workflows/{id}` — там стоял голый
    `DELETE FROM workflow_definitions` и безусловный `{"ok": true}`. Удаление
    чужого или уже удалённого сценария отвечало «🗑 Удалён», список
    перезагружался и показывал сценарий на месте. А прогоны оставались: в
    развёрнутой схеме (schema_v183.sql) `workflow_runs.workflow_id` стоит с
    `ON DELETE SET NULL`, так что незавершённый прогон не падал ошибкой, а
    навсегда оставался в состоянии `running` и терял имя сценария. Готовый
    `delete_workflow` в движке именно это и разбирает — и звал его только
    маршрут `DELETE /workflow/{id}`, которого нет во фронте.
  * Пауза и активация переписывали тот же UPDATE, что `_set_active`.

ЧТО ЛОМАЛОСЬ В ЯЗЫКЕ ШАГОВ. Экран рисует шаг `{"type": ...}` и только его.
`POST /workflow/create` требовал вместо этого ключ `action` и про `type` не
знал, а `POST /workflows/{id}/steps` принимал любой непустой `type`. Шаг без
`type` ронял ВЕСЬ экран деталей: `s.type.toUpperCase()` давал TypeError, он
уходил в общий catch, и вместо сценария показывалась ошибка. Шаг с опечаткой в
типе становился безымянной точкой. Словарь допустимых типов лежал в движке с
комментарием ровно про это — и не вызывался ниоткуда.

ЧТО ЛОМАЛОСЬ В СОСТОЯНИИ. `GET /workflow/{id}/status` передавал id СЦЕНАРИЯ в
аргумент, который означает id ПРОГОНА (`workflow_runs.id`), и отдавал чужой
прогон как состояние этого сценария; без совпадения id — тело `null` с кодом
200. Блок «📊 Последние запуски» был написан по полю `runs`, которого
обработчик деталей не отдавал: разметка не показывалась никогда.

Чего тут НЕ проверяется, потому что этого нет: исполнения шагов. Сценарий
по-прежнему только сохраняется — и ответ запуска обязан это говорить, см.
`test_the_launch_does_not_claim_the_steps_ran`.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M
from services import workflow_engine as WF

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "services" / "mini_app_workflows.py"
INDEX = ROOT / "mini_app" / "index.html"
UID = 515151


# ── Заглушки ─────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, pool, deleted: int):
        self._pool = pool
        self._deleted = deleted

    async def execute(self, q, *a):
        self._pool.calls.append((q, a))
        if q.lstrip().upper().startswith("DELETE"):
            return f"DELETE {self._deleted}"
        return "UPDATE 1"

    def transaction(self):
        return _Null()


class _Null:
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False


class _Acquire(_Null):
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn


_MISSING = object()


class _Pool:
    """Пул, который считает запросы и умеет отдать «ничего не затронуто»."""

    # Поля перечислены ровно те, что выбирает запрос деталей: обращение к
    # строке идёт по ключу, и заглушка, отставшая от запроса, даёт KeyError на
    # ВЕРНОМ коде. Именно так однажды покраснела ветка на колонке postponed.
    _DEFAULT_ROW = {"id": 7, "name": "Сценарий", "description": "",
                    "steps": [], "is_active": False,
                    "bot_id": None, "bot_username": None}

    def __init__(self, *, row=_MISSING, rows=None, affected=1, deleted=1):
        self.calls: list[tuple[str, tuple]] = []
        # row=None означает «такой строки нет» — отличать его от «не задано»
        # обязательно, иначе тест на чужой сценарий молча проверяет свой.
        self._row = self._DEFAULT_ROW if row is _MISSING else row
        self._rows = rows or []
        self._affected = affected
        self._deleted = deleted

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return list(self._rows)

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        if "RETURNING id" in q:
            return {"id": 7}
        return self._row

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return 7

    async def execute(self, q, *a):
        self.calls.append((q, a))
        head = q.lstrip().split(" ", 1)[0].upper()
        return f"{head} {self._affected}"

    def acquire(self):
        return _Acquire(_Conn(self, self._deleted))


class _Req:
    def __init__(self, body=None, **match):
        self._body = body if body is not None else {}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.rel_url = type("U", (), {"query": {}})()
        self.match_info = {k: str(v) for k, v in match.items()}
        self.method = "POST"

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def _call(pool, method: str, path: str, req: _Req):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        rpath = info.get("path") or info.get("formatter") or ""
        if route.method == method and rpath == path:
            return asyncio.run(route.handler(req))
    raise AssertionError(f"маршрут {method} {path} не зарегистрирован")


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


# ── Удаление ─────────────────────────────────────────────────────────────────

def test_deleting_a_workflow_that_is_not_yours_is_not_found():
    """Чужой или уже удалённый id — 404, а не «🗑 Удалён»."""
    pool = _Pool(deleted=0)
    resp = _call(pool, "DELETE", "/api/miniapp/workflows/{wf_id}",
                 _Req(wf_id=7))
    assert resp.status == 404, (
        "удаление чужого сценария отвечало успехом: владелец видел «Удалён», "
        f"а список показывал сценарий на месте (ответ {resp.status})")


def test_deleting_clears_the_run_history_first():
    """Прогоны отвязываются до DELETE — иначе внешний ключ ронял удаление."""
    pool = _Pool(deleted=1)
    resp = _call(pool, "DELETE", "/api/miniapp/workflows/{wf_id}",
                 _Req(wf_id=7))
    assert resp.status == 200
    queries = [q for q, _ in pool.calls]
    updates = [i for i, q in enumerate(queries) if "workflow_runs" in q]
    deletes = [i for i, q in enumerate(queries)
               if "DELETE FROM workflow_definitions" in q]
    assert updates and deletes, (
        "удаление идёт мимо движка: прогоны не тронуты, внешний ключ "
        f"workflow_runs.workflow_id уронит запрос. Запросы: {queries}")
    assert max(updates) < min(deletes), "прогоны разбираются после удаления"
    assert any("cancelled" in q for q in queries), (
        "незавершённые прогоны не отменены — по ON DELETE SET NULL они "
        "останутся «выполняющимися» и без имени сценария навсегда")


def test_both_delete_routes_lead_to_the_same_handler():
    """Две кнопки одного действия не должны означать разное."""
    app = web.Application()
    M.setup_routes(app, _Pool())
    handlers = {}
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if route.method == "DELETE" and path in (
                "/api/miniapp/workflows/{wf_id}", "/api/miniapp/workflow/{wf_id}"):
            handlers[path] = route.handler
    assert len(handlers) == 2, f"не оба маршрута удаления на месте: {handlers}"
    assert len(set(handlers.values())) == 1, (
        "у удаления снова две реализации — расходиться они начинают сразу")


# ── Пауза и активация ────────────────────────────────────────────────────────

def test_pausing_a_workflow_that_is_not_yours_is_not_found():
    pool = _Pool(affected=0)
    resp = _call(pool, "PATCH", "/api/miniapp/workflows/{wf_id}",
                 _Req({"active": False}, wf_id=7))
    assert resp.status == 404


def test_pause_and_the_screen_flip_the_same_flag():
    """PATCH и POST /pause — один и тот же путь, не две копии UPDATE."""
    for method, path, req in (
            ("PATCH", "/api/miniapp/workflows/{wf_id}",
             _Req({"active": False}, wf_id=7)),
            ("POST", "/api/miniapp/workflow/{wf_id}/pause", _Req(wf_id=7)),
    ):
        pool = _Pool(affected=1)
        resp = _call(pool, method, path, req)
        assert resp.status == 200, f"{method} {path} → {resp.status}"
        assert _body(resp)["active"] is False
        assert any("is_active=$1" in q for q, _ in pool.calls), (
            f"{method} {path} пишет паузу своим запросом")


# ── Язык шагов ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("step,why", [
    ({"type": "выдумка", "text": "раз"}, "неизвестный тип рисуется точкой без имени"),
    ({"text": "без типа"}, "шаг без type ронял весь экран деталей"),
    ({"type": "message"}, "сообщение без текста нечего отправлять"),
    ({"type": "delay", "delay_minutes": 0}, "нулевая задержка — не задержка"),
    ({"type": "delay", "delay_minutes": -5}, "отрицательная задержка"),
    ({"type": "delay", "delay_minutes": 10 ** 9}, "задержка на две тысячи лет"),
    ({"type": "delay"}, "задержка без числа минут"),
    ({"type": "condition"}, "условие без условия"),
    ({"type": "webhook", "url": "http://example.com/x"},
     "вебхук по http уйдёт открытым текстом"),
    ({"type": "message", "text": "я" * (WF.MAX_STEP_TEXT + 1)},
     "текст длиннее предела сообщения Telegram"),
])
def test_a_broken_step_is_refused(step, why):
    pool = _Pool(row={"steps": []})
    resp = _call(pool, "POST", "/api/miniapp/workflows/{wf_id}/steps",
                 _Req(step, wf_id=7))
    assert resp.status == 400, f"шаг принят, хотя {why}: {step}"
    assert not any("UPDATE workflow_definitions" in q for q, _ in pool.calls), (
        f"шаг записан в базу, хотя {why}")


@pytest.mark.parametrize("step", [
    {"type": "message", "text": "Привет"},
    {"type": "delay", "delay_minutes": 60},
    {"type": "condition", "condition": "subscribed", "condition_value": "x"},
    {"type": "webhook", "url": "https://example.com/hook"},
    {"type": "action", "action": "warmup"},
])
def test_a_good_step_is_saved(step):
    """Обратный контроль: проверка не запрещает то, что экран умеет прислать."""
    pool = _Pool(row={"steps": []})
    resp = _call(pool, "POST", "/api/miniapp/workflows/{wf_id}/steps",
                 _Req(dict(step), wf_id=7))
    assert resp.status == 200, f"нормальный шаг отвергнут: {_body(resp)}"
    assert any("UPDATE workflow_definitions" in q for q, _ in pool.calls)


def test_a_step_added_to_a_workflow_that_is_not_yours_is_not_found():
    pool = _Pool(row=None)
    resp = _call(pool, "POST", "/api/miniapp/workflows/{wf_id}/steps",
                 _Req({"type": "message", "text": "раз"}, wf_id=7))
    assert resp.status == 404


def test_the_number_of_steps_is_bounded():
    """Шаги лежат одной jsonb-колонкой и читаются целиком на каждом открытии."""
    pool = _Pool(row={"steps": [{"type": "message", "text": "раз"}]
                               * WF.MAX_WORKFLOW_STEPS})
    resp = _call(pool, "POST", "/api/miniapp/workflows/{wf_id}/steps",
                 _Req({"type": "message", "text": "ещё"}, wf_id=7))
    assert resp.status == 400, "сценарий растёт без предела"


def test_create_refuses_steps_in_the_old_action_language():
    """`POST /workflow/create` требовал ключ `action` — такой шаг ронял экран."""
    pool = _Pool()
    resp = _call(pool, "POST", "/api/miniapp/workflow/create",
                 _Req({"name": "Сценарий",
                       "steps": [{"action": "post", "target": "channel"}]}))
    assert resp.status == 400, (
        "шаг без type сохранён: экран деталей упадёт на undefined.toUpperCase")


def test_create_accepts_the_language_the_screen_speaks():
    pool = _Pool()
    resp = _call(pool, "POST", "/api/miniapp/workflow/create",
                 _Req({"name": "Сценарий",
                       "steps": [{"type": "message", "text": "Привет"},
                                 {"type": "delay", "delay_minutes": 30}]}))
    assert resp.status == 200, _body(resp)
    assert _body(resp)["steps"] == 2


def test_every_template_passes_the_same_check_as_a_hand_made_step():
    """Шаблон, разошедшийся со словарём, нарисовал бы «⚫ undefined» так же."""
    for key in WF.WORKFLOW_TEMPLATES:
        steps = WF.workflow_template(key)["steps"]
        WF.validate_steps(steps)   # ValueError = шаблон врёт экрану


def test_a_new_workflow_is_not_switched_on():
    """Включать то, что не исполняется, значит обещать несуществующую работу."""
    pool = _Pool()
    _call(pool, "POST", "/api/miniapp/workflows",
          _Req({"name": "Сценарий", "template": "welcome"}))
    inserts = [a for q, a in pool.calls if "INSERT INTO workflow_definitions" in q]
    assert inserts, "сценарий не создан"
    assert False in inserts[0], f"сценарий создан включённым: {inserts[0]}"


# ── Состояние и запуск ───────────────────────────────────────────────────────

def test_the_detail_screen_gets_the_runs_it_renders():
    pool = _Pool(rows=[{"id": 3, "status": "cancelled", "current_step": 0,
                        "total_steps": 2, "started_at": None,
                        "finished_at": None, "error_message": None}])
    resp = _call(pool, "GET", "/api/miniapp/workflows/{wf_id}", _Req(wf_id=7))
    data = _body(resp)
    assert "runs" in data, (
        "блок «Последние запуски» написан по полю runs — обработчик его не "
        "отдавал, и разметка не показывалась никогда")
    assert data["runs"] and data["runs"][0]["status"] == "cancelled"


def test_the_status_asks_about_the_workflow_not_about_a_run_with_the_same_id():
    pool = _Pool(rows=[], row={"id": 7})
    _call(pool, "GET", "/api/miniapp/workflow/{wf_id}/status", _Req(wf_id=7))
    run_queries = [q for q, _ in pool.calls if "workflow_runs" in q]
    assert run_queries, "состояние сценария не спрашивает про прогоны вовсе"
    assert any("wr.workflow_id = $1" in q for q in run_queries), (
        "id сценария подставляется как id прогона — отдаётся чужой прогон:\n"
        + "\n".join(run_queries))


def test_a_workflow_never_started_says_so_instead_of_a_null_body():
    pool = _Pool(row={"id": 7})

    async def _none(q, *a):
        pool.calls.append((q, a))
        # Запрос прогона тоже упоминает workflow_definitions (LEFT
        # JOIN за именем сценария) — различать надо по целевой таблице.
        return None if "FROM workflow_runs" in q else {"id": 7}

    pool.fetchrow = _none
    resp = _call(pool, "GET", "/api/miniapp/workflow/{wf_id}/status",
                 _Req(wf_id=7))
    assert resp.status == 200
    data = _body(resp)
    assert data.get("run") is None and data.get("ok") is True, (
        f"вместо ответа отдано {data!r} — раньше здесь было тело null")


def test_the_status_of_someone_elses_workflow_is_not_found():
    pool = _Pool(row=None)
    resp = _call(pool, "GET", "/api/miniapp/workflow/{wf_id}/status",
                 _Req(wf_id=7))
    assert resp.status == 404


def test_the_launch_of_a_missing_workflow_is_not_a_success():
    pool = _Pool(row=None)
    resp = _call(pool, "POST", "/api/miniapp/workflow/{wf_id}/execute",
                 _Req(wf_id=7))
    assert resp.status == 404, (
        "раньше ответ собирался как {'ok': True, **result}: отказ уходил "
        "кодом 200, и экран показывал успех на несуществующем сценарии")


def test_the_launch_does_not_claim_the_steps_ran():
    """Исполнителя сценариев нет — ответ обязан это говорить."""
    pool = _Pool(row={"id": 7, "steps": [{"type": "message", "text": "раз"}]})
    resp = _call(pool, "POST", "/api/miniapp/workflow/{wf_id}/execute",
                 _Req(wf_id=7))
    data = _body(resp)
    assert data.get("executed") is False, f"ответ обещает исполнение: {data}"
    assert "не выполняются" in (data.get("note") or ""), data


# ── Храповики ────────────────────────────────────────────────────────────────

def _handlers() -> dict[str, str]:
    src = MODULE.read_text(encoding="utf-8")
    lines = src.split("\n")
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # setup_routes — внешняя функция, её тело включает все остальные;
            # без исключения любая проверка «в теле обработчика» видела бы в
            # ней весь модуль сразу.
            if node.name == "setup_routes":
                continue
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def test_the_measurer_sees_the_handlers():
    found = _handlers()
    assert len(found) >= 12, f"обработчики не разобрались: {sorted(found)}"


def test_every_write_path_validates_the_steps():
    """Любой обработчик, пишущий шаги, зовёт один и тот же словарь."""
    offenders = []
    for name, body in _handlers().items():
        writes_steps = ("steps=steps" in body
                        or "SET steps" in body
                        or "steps = COALESCE(steps" in body)
        if writes_steps and "validate_steps(" not in body:
            offenders.append(name)
    assert not offenders, (
        "шаги пишутся без проверки словарём — вернётся «⚫ undefined» на "
        f"экране: {offenders}")


def test_no_handler_writes_the_workflow_tables_by_hand():
    """Запись в сценарии — только через движок (кроме аппенда одного шага).

    Именно рукописные копии INSERT/UPDATE/DELETE рядом с маршрутом и разошлись
    с движком: удаление без отвязки прогонов, пауза без 404.
    """
    offenders = []
    for name, body in _handlers().items():
        if name == "workflow_add_step":
            continue   # аппенд в jsonb одной строкой, в движке аналога нет
        for verb in ("INSERT INTO workflow_definitions",
                     "UPDATE workflow_definitions",
                     "DELETE FROM workflow_definitions",
                     "UPDATE workflow_runs"):
            if verb in body:
                offenders.append(f"{name}: {verb}")
    assert not offenders, (
        "обработчик снова пишет в сценарии сам, мимо workflow_engine:\n  "
        + "\n  ".join(offenders))


def test_the_old_handlers_are_gone_from_mini_app_api():
    """Переезд должен быть переездом, а не копией."""
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    for name in ("async def workflow_list", "async def workflow_create_plural",
                 "async def workflow_detail_plural", "async def workflow_execute"):
        assert name not in src, f"в mini_app_api.py осталась копия: {name}"
    assert "mini_app_workflows.setup_routes" in src, "модуль не подключён"


# ── Экран ────────────────────────────────────────────────────────────────────

def test_the_screen_does_not_fall_on_a_step_without_a_type():
    html = INDEX.read_text(encoding="utf-8")
    assert "s.type.toUpperCase()" not in html, (
        "undefined.toUpperCase() — TypeError, он уходит в общий catch, и "
        "вместо сценария экран показывает ошибку")
    assert "typeof s.type === 'string'" in html, "защиты от шага без типа нет"


def test_the_screen_names_a_cancelled_run_cancelled():
    html = INDEX.read_text(encoding="utf-8")
    i = html.index("const runNames = {")
    names = html[i:html.index("}", i)]
    for status in ("cancelled", "pending", "running", "failed"):
        assert status in names, (
            f"прогон в состоянии {status} попадёт в ветку «иначе» и покажется "
            "как «🔵 В работе»")


# ── Бот сценария ─────────────────────────────────────────────────────────────

def test_the_chosen_bot_is_saved():
    """Модалка создания присылает bot_id — раньше он выбрасывался молча."""
    pool = _Pool()
    resp = _call(pool, "POST", "/api/miniapp/workflows",
                 _Req({"name": "Сценарий", "bot_id": 999}))
    assert resp.status == 200, _body(resp)
    inserts = [a for q, a in pool.calls if "INSERT INTO workflow_definitions" in q]
    assert inserts, "сценарий не создан"
    assert 999 in inserts[0], (
        f"выбранный бот не доехал до INSERT: {inserts[0]} — владелец выбирал "
        "бота, получал «✅ создан», и выбор исчезал без следа")


def test_an_unbound_workflow_is_allowed():
    """Селект предлагает «Не привязан» — пустая строка это не ноль и не ошибка."""
    pool = _Pool()
    resp = _call(pool, "POST", "/api/miniapp/workflows",
                 _Req({"name": "Сценарий", "bot_id": ""}))
    assert resp.status == 200, _body(resp)
    assert _body(resp)["bot_id"] is None


def test_someone_elses_bot_cannot_be_bound():
    """Иначе по номеру бота можно повесить свой сценарий на бота соседа."""
    pool = _Pool(row=None)
    resp = _call(pool, "POST", "/api/miniapp/workflows",
                 _Req({"name": "Сценарий", "bot_id": 999}))
    assert resp.status == 404, (
        f"чужой бот привязан без проверки владельца (ответ {resp.status})")
    assert not any("INSERT INTO workflow_definitions" in q for q, _ in pool.calls)


def test_the_bot_check_is_scoped_to_the_owner():
    pool = _Pool()
    _call(pool, "POST", "/api/miniapp/workflows",
          _Req({"name": "Сценарий", "bot_id": 999}))
    checks = [q for q, _ in pool.calls if "managed_bots" in q]
    assert checks, "владелец бота не проверяется вовсе"
    assert any("added_by=$2" in q for q in checks), (
        f"проверка бота без владельца: {checks}")


def test_a_bad_bot_id_is_refused():
    pool = _Pool()
    resp = _call(pool, "POST", "/api/miniapp/workflows",
                 _Req({"name": "Сценарий", "bot_id": "не-число"}))
    assert resp.status == 400


def test_the_detail_screen_shows_which_bot_it_is():
    pool = _Pool(row={"id": 7, "name": "Сценарий", "description": "",
                      "steps": [], "is_active": False, "bot_id": 999,
                      "bot_username": "my_bot"})
    data = _body(_call(pool, "GET", "/api/miniapp/workflows/{wf_id}",
                       _Req(wf_id=7)))
    assert data["bot_id"] == 999 and data["bot_username"] == "my_bot", data


def test_the_screen_prints_the_bound_bot():
    html = INDEX.read_text(encoding="utf-8")
    assert "d.bot_username" in html, (
        "экран деталей не показывает бота — выбор владельца снова некуда "
        "посмотреть")


# ── Плитки «Выполняется» и «Ошибки» ──────────────────────────────────────────

def test_the_list_reports_the_state_of_the_last_run():
    pool = _Pool(rows=[{"id": 7, "name": "Сценарий", "status": "paused",
                        "step_count": 2, "last_run": None,
                        "run_status": "failed"}])
    data = _body(_call(pool, "GET", "/api/miniapp/workflows", _Req()))
    wf = data["workflows"][0]
    assert wf["run_status"] == "failed", wf
    assert wf["status"] == "paused", (
        "состояние определения и состояние прогона — разные вещи, в одном "
        "поле они и слиплись")


def test_the_tiles_count_runs_and_not_the_definition():
    html = INDEX.read_text(encoding="utf-8")
    i = html.index("document.getElementById('wf-running')")
    seg = html[i:i + 400]
    assert "run_status === 'running'" in seg, (
        "плитка «Выполняется» считалась по w.status, где бывает только "
        "active/paused — она показывала ноль при любых данных")
    assert "run_status === 'failed'" in html[i:i + 600], (
        "плитка «Ошибки» считалась по тому же полю и так же всегда нулевая")
