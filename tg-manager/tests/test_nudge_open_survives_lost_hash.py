"""Регресс: «Открыть» из нуджа доходит до раздела, даже когда Telegram стёр хэш.

Первопричина (жалоба владельца «Открыть открывает не саму проблему, а главную»).
Кнопка «Открыть» в нудже — web_app-ссылка с разделом в #<kind>
(`services/organism/runner._open_button`). На ЧАСТИ клиентов Telegram стирает наш
хэш и дописывает вместо него свои tgWebApp*-параметры: `location.hash` на старте
мини-аппа не содержит kind, парсер получает пусто, и приложение остаётся на
ГЛАВНОЙ. Проверено симуляцией парсера: хэш `#vlayer&tgWebAppData=…` разворачивается
в `vlayer`, а хэш `#tgWebAppData=…` (наш сегмент стёрт) — в пусто.

Спасательный путь, не зависящий от поведения клиента Telegram: организм на
отправке нуджа дублирует цель «Открыть» в своём состоянии (ключ `nudge_open`), а
мини-апп на старте, не найдя раздела в хэше, забирает её с сервера — РАЗОВО (сервер
сразу чистит ключ) и только в узком окне (30 мин), чтобы обычный запуск приложения
спустя час не прыгал в старый нудж.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from services.organism import brain, runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakePool:
    def __init__(self):
        self._state = {}

    async def fetchrow(self, sql, *a):
        if "organism_state" in sql:
            v = self._state.get((a[0], a[1]))
            return {"value": v} if v is not None else None
        return None

    async def execute(self, sql, *a):
        if "organism_state" in sql:
            self._state[(a[0], a[1])] = a[2]
        return "OK"

    async def fetch(self, sql, *a):
        return []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_pulse(monkeypatch, suggestions):
    async def fake_pulse(pool, owner_id):
        return {"narrative": "", "snapshot": {}, "suggestions": suggestions}
    monkeypatch.setattr(brain, "pulse", fake_pulse)


def _stored_open(pool, owner_id):
    raw = pool._state.get((owner_id, "nudge_open"))
    return json.loads(raw) if raw is not None else None


# ── Организм кладёт цель «Открыть» в состояние ────────────────────────────────

def test_nudge_stores_open_target(monkeypatch):
    # «4 ходят по кругу» — это kind=vlayer; цель обязана попасть в состояние,
    # иначе мини-аппу на старте нечего восстанавливать.
    _patch_pulse(monkeypatch, [{"id": "vlayer_bouncing", "severity": "warn",
                                "title": "4 ходят по кругу", "why": "...",
                                "action": {"kind": "vlayer"}}])
    pool = _FakePool()

    async def notifier(o, m):
        pass
    assert _run(runner._tick_owner(pool, None, 7, notifier=notifier, now=1000.0)) is True
    got = _stored_open(pool, 7)
    assert got and got.get("kind") == "vlayer" and got.get("at") == 1000.0
    assert "param" not in got            # у vlayer параметра нет


def test_nudge_open_carries_op_id(monkeypatch):
    # Нудж об операции ведёт к КОНКРЕТНОЙ операции — param обязан сохраниться
    # (той же формы, что разворачивает _open_button: kind + op_id).
    _patch_pulse(monkeypatch, [{"id": "operation", "severity": "urgent",
                                "title": "Операция упала", "why": "...",
                                "action": {"kind": "operation", "op_id": 42}}])
    pool = _FakePool()

    async def notifier(o, m):
        pass
    assert _run(runner._tick_owner(pool, None, 5, notifier=notifier, now=2000.0)) is True
    got = _stored_open(pool, 5)
    assert got.get("kind") == "operation" and got.get("param") == "42"


def test_no_action_leaves_no_open_target(monkeypatch):
    # Подсказка без действия (action={}) не должна оставлять «Открыть»-цель:
    # иначе мини-апп прыгнул бы в раздел, которого у этого нуджа нет.
    _patch_pulse(monkeypatch, [{"id": "x", "severity": "urgent",
                                "title": "X", "why": "y", "action": {}}])
    pool = _FakePool()

    async def notifier(o, m):
        pass
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=1000.0)) is True
    assert _stored_open(pool, 1) is None


# ── Эндпойнт восстановления: разовый расход + окно ───────────────────────────

def _call_pending_open(pool, uid, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from services import mini_app_api, mini_app_parser
    # Эндпойнт — замыкание внутри setup_routes и берёт _get_uid из mini_app_api
    # в момент регистрации: патчим ДО setup_routes, чтобы замыкание поймало его.
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: uid, raising=True)
    app = web.Application()
    mini_app_parser.setup_routes(app, pool)
    handler = None
    for r in app.router.routes():
        res = r.resource
        if res is not None and res.canonical == "/api/miniapp/organism/pending_open":
            handler = r.handler
            break
    assert handler is not None, "маршрут pending_open не зарегистрирован"
    req = make_mocked_request("POST", "/api/miniapp/organism/pending_open")
    resp = _run(handler(req))
    return json.loads(resp.body.decode("utf-8"))


def test_pending_open_returns_then_consumes(monkeypatch):
    pool = _FakePool()
    pool._state[(1, "nudge_open")] = json.dumps({"kind": "vlayer", "at": time.time()})
    first = _call_pending_open(pool, 1, monkeypatch)
    assert first.get("kind") == "vlayer"
    # второй заход — пусто: расход разовый, иначе обычный запуск прыгал бы снова
    second = _call_pending_open(pool, 1, monkeypatch)
    assert second == {}


def test_pending_open_passes_param(monkeypatch):
    pool = _FakePool()
    pool._state[(1, "nudge_open")] = json.dumps(
        {"kind": "operation", "param": "42", "at": time.time()})
    got = _call_pending_open(pool, 1, monkeypatch)
    assert got.get("kind") == "operation" and got.get("param") == "42"


def test_pending_open_ignores_stale(monkeypatch):
    pool = _FakePool()
    pool._state[(1, "nudge_open")] = json.dumps(
        {"kind": "vlayer", "at": time.time() - 3600})     # час назад
    got = _call_pending_open(pool, 1, monkeypatch)
    assert got == {}                                       # старше 30 мин — не «я нажал Открыть»
    # и всё равно израсходован, чтобы не всплыть позже
    assert pool._state.get((1, "nudge_open")) in (None, "null")


def test_pending_open_empty_when_nothing(monkeypatch):
    pool = _FakePool()
    assert _call_pending_open(pool, 1, monkeypatch) == {}


# ── Мини-апп: на старте без хэша зовёт спасательный путь ──────────────────────

def test_boot_recovers_when_hash_lost():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    # нет раздела в хэше → зовём восстановление, а не молчим на главной
    assert "else setTimeout(recoverNudgeOpen" in html
    # спасательный путь спрашивает сервер и разворачивает цель тем же runPulseAction
    assert "async function recoverNudgeOpen" in html
    assert "/api/miniapp/organism/pending_open" in html
    assert "runPulseAction({kind: a.kind" in html


def test_pending_open_route_registered():
    src = open(os.path.join(ROOT, "services", "mini_app_parser.py"), encoding="utf-8").read()
    assert 'add_post("/api/miniapp/organism/pending_open"' in src
