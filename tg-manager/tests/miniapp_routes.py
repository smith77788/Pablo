"""Список зарегистрированных маршрутов мини-аппа — один способ его получить.

Нужен и храповику (`test_miniapp_routes_are_not_lost.py`), и генератору слепка.
Держим в одном месте, чтобы слепок и проверка не считали по-разному.
"""
from __future__ import annotations

from aiohttp import web


class StubPool:
    """Пул-заглушка: регистрация маршрутов в базу не ходит."""

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        return "OK"


def registered_routes() -> list[str]:
    """Все маршруты как «МЕТОД путь», отсортированные."""
    from services import mini_app_api as M

    app = web.Application()
    M.setup_routes(app, StubPool())
    out = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or info.get("prefix") or ""
        out.add(f"{route.method} {path}")
    return sorted(out)
