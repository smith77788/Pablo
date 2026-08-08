"""Регресс dead-route: каждый фронтовый вызов api('/api/miniapp/...') обязан
иметь зарегистрированный бэкенд-маршрут (иначе кнопка отдаёт 404 — «мёртвая»).

Найдено этим сканом (2026-07-12): фронт звал `/broadcasts/schedule` (мн.ч.) и
`/accounts/check_all`, а бэк регистрировал только `/broadcast/schedule` (ед.ч.) и
`/accounts/check` → обе кнопки 404. Плюс контракт-рассинхрон полей у планировщика
рассылки (message_text/scheduled_at vs text/scheduled_for). Исправлено алиасами
маршрутов + приёмом обоих контрактов полей.

Матчер учитывает конкатенацию (`api('/api/miniapp/x/'+id)`): для литерала-
префикса, оканчивающегося на '/', достаточно наличия маршрута, начинающегося с
этого префикса (параметризованного `{id}`).
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_FRONT = [os.path.join(_ROOT, "mini_app", "index.html")] + glob.glob(
    os.path.join(_ROOT, "mini_app", "screens", "*.js")
)
_API = os.path.join(_ROOT, "services", "mini_app_api.py")

_CALL = re.compile(r"""(?:api|fetch)\(\s*[`'"](/api/miniapp/[^`'"?]+)""")


def _frontend_calls() -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {}
    for f in _FRONT:
        if not os.path.exists(f):
            continue
        src = open(f, encoding="utf-8", errors="replace").read()
        for m in _CALL.finditer(src):
            calls.setdefault(m.group(1), set()).add(os.path.basename(f))
    return calls


def _routes() -> set[str]:
    api = open(_API, encoding="utf-8").read()
    routes = set(re.findall(
        r"""router\.add_(?:get|post|put|delete|patch)\(\s*[`'"](/api/miniapp/[^`'"]+)""", api))
    routes |= set(re.findall(
        r"""add_route\(\s*[`'"][A-Z]+[`'"]\s*,\s*[`'"](/api/miniapp/[^`'"]+)""", api))
    return routes


def _matched(path: str, routes: set[str], route_pats: list) -> bool:
    if any(p.match(path) for p in route_pats):
        return True
    if path.endswith("/"):  # конкатенация: literal-префикс + переменный хвост
        for r in routes:
            idx = r.find("{")
            if idx > 0 and r[:idx] == path:
                return True
            if r.startswith(path):
                return True
    return False


def test_no_dead_frontend_routes():
    calls = _frontend_calls()
    routes = _routes()
    route_pats = [
        re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(r).replace(r"\{", "{").replace(r"\}", "}")) + "$")
        for r in routes
    ]
    missing = {
        p: sorted(fs) for p, fs in calls.items()
        if not _matched(p, routes, route_pats)
    }
    assert not missing, f"фронтовые вызовы без бэкенд-маршрута (404): {missing}"


def test_specific_aliases_registered():
    routes = _routes()
    assert "/api/miniapp/broadcasts/schedule" in routes
    assert "/api/miniapp/accounts/check_all" in routes


def test_broadcast_schedule_accepts_both_field_contracts():
    api = open(_API, encoding="utf-8").read()
    m = re.search(r"async def broadcast_schedule\(.*?\n(.*?)\n    async def ", api, re.DOTALL)
    assert m, "broadcast_schedule handler not found"
    body = m.group(1)
    assert 'body.get("text") or body.get("message_text")' in body, (
        "должен принимать и text, и message_text (контракт фронта)"
    )
    assert 'body.get("scheduled_for") or body.get("scheduled_at")' in body, (
        "должен принимать и scheduled_for, и scheduled_at"
    )
