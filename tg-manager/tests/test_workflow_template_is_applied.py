"""«Создать воркфлоу по шаблону» создавал ПУСТОЙ сценарий.

На экране «🔄 Воркфлоу» четыре шаблона: 👋 Приветствие, 💧 Дрип-серия,
⚡ Реакция на событие, 🔀 Условная логика. Нажатие спрашивало подтверждение
(«Создать воркфлоу по шаблону "💧 Дрип-серия"?»), отвечало «✅ Воркфлоу по
шаблону создан» — а обработчик параметр `template` не читал:

    "INSERT INTO workflow_definitions(owner_id, name, description, steps, is_active) "
    "VALUES($1,$2,$3,'[]'::jsonb, FALSE)"

То есть у человека появлялся сценарий из НУЛЯ шагов с названием шаблона.

Второе, что здесь закреплено, — честность экрана. Сценарий сегодня нигде не
исполняется: `execute_workflow` только заводит строку в `workflow_runs`, шаги не
выполняет никто, и по `is_active` тоже не срабатывает ничего. Пока это так,
экран обязан говорить об этом прямо, иначе «🟢 Активен» читается как «работает».
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M
from services import workflow_engine as WF

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
UID = 626262


class _Pool:
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
        return 55

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, body: dict):
        self._body = body
        self.rel_url = type("U", (), {"query": {}})()
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.method = "POST"
        self.match_info: dict[str, str] = {}

    async def json(self):
        return self._body


def _create(pool, body: dict):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if route.method == "POST" and path == "/api/miniapp/workflows":
            return asyncio.run(route.handler(_Req(body)))
    raise AssertionError("роут создания воркфлоу не зарегистрирован")


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def test_every_offered_template_exists():
    """Экран предлагает четыре — все четыре должны быть описаны на сервере."""
    html = INDEX.read_text("utf-8")
    i = html.index("const tplNames = {")
    offered = html[i:html.index("}", i)]
    for key in ("welcome", "drip", "react", "condition"):
        assert key in offered, f"{key} исчез из экрана"
        assert WF.workflow_template(key), f"шаблон {key} не описан на сервере"


def test_an_unknown_template_has_no_description():
    assert WF.workflow_template("нет-такого") is None
    assert WF.workflow_template("") is None


@pytest.mark.parametrize("key", ["welcome", "drip", "react", "condition"])
def test_template_steps_are_shaped_like_the_screen_expects(key):
    tpl = WF.workflow_template(key)
    assert len(tpl["steps"]) >= 2, f"в шаблоне {key} меньше двух шагов"
    for step in tpl["steps"]:
        assert step.get("type") in WF.WORKFLOW_STEP_TYPES, (
            f"шаг типа {step.get('type')} экран показать не умеет")
        if step["type"] == "message":
            assert step.get("text"), "шаг-сообщение без текста"
        if step["type"] == "delay":
            assert isinstance(step.get("delay_minutes"), int) and step["delay_minutes"] > 0
        if step["type"] == "condition":
            assert step.get("condition"), "шаг-условие без условия"


def test_template_text_is_russian():
    """Тексты шаблона человек увидит в шагах и отправит людям."""
    import re

    for key in WF.WORKFLOW_TEMPLATES:
        for step in WF.workflow_template(key)["steps"]:
            text = step.get("text") or ""
            if not text:
                continue
            assert re.search("[а-яёА-ЯЁ]", text), f"{key}: текст не на русском: {text!r}"
            assert not re.search("[A-Za-z]{4}", text), f"{key}: английские слова: {text!r}"
            assert not re.search("[　-鿿]", text), f"{key}: чужая графика: {text!r}"


def test_creating_with_a_template_stores_its_steps():
    pool = _Pool()
    d = _body(_create(pool, {"name": "Дрип", "template": "drip"}))
    assert d["ok"] is True
    assert d["steps"] == len(WF.workflow_template("drip")["steps"]) > 0, (
        f"шаблон не применён: {d}")
    inserts = [(q, a) for q, a in pool.calls if "INSERT INTO workflow_definitions" in q]
    assert inserts, "сценарий не создан"
    q, args = inserts[0]
    assert "'[]'::jsonb" not in q, "шаги снова вставляются пустым литералом"
    stored = next((a for a in args if isinstance(a, str) and a.startswith("[")), None)
    assert stored, f"шаги не доехали параметром: {args}"
    assert len(json.loads(stored)) == d["steps"]


def test_creating_without_a_template_stays_empty():
    pool = _Pool()
    d = _body(_create(pool, {"name": "Свой"}))
    assert d["ok"] is True and d["steps"] == 0


def test_an_unknown_template_is_refused():
    pool = _Pool()
    r = _create(pool, {"name": "Сценарий", "template": "выдуманный"})
    assert r.status == 400, "неизвестный шаблон молча дал пустой сценарий"
    assert not any("INSERT INTO workflow_definitions" in q for q, _ in pool.calls)


def test_the_screen_does_not_promise_execution_that_does_not_exist():
    """Пока шаги не исполняет никто, «Активен» нельзя оставлять без пояснения."""
    html = INDEX.read_text("utf-8")
    i = html.index('<div class="hdr-title">🔄 Воркфлоу</div>')
    head = html[i:i + 2000]
    assert "пока не выполняется" in head, (
        "с экрана исчезло предупреждение, что сценарий не исполняется — "
        "«🟢 Активен» снова читается как «работает»")


def test_execution_is_still_only_a_record_so_the_warning_is_true():
    """Страховка от обратного: появится настоящий исполнитель — предупреждение
    надо снять, и тест об этом напомнит."""
    src = Path(WF.__file__).read_text("utf-8")
    i = src.index("async def execute_workflow")
    body = src[i:src.index("async def get_workflow_status")]
    assert "INSERT INTO workflow_runs" in body
    for marker in ("send_message", "operation_bus", "submit("):
        assert marker not in body, (
            "в execute_workflow появилось настоящее исполнение шагов — пора "
            "убрать с экрана предупреждение и поправить этот тест")
