"""Ратчет: ни один роут мини-аппа не работает без токена.

Из 628 зарегистрированных роутов 243 не упоминались ни в одном тесте, то есть
проверку «а спрашивает ли этот эндпоинт авторизацию» никто за них не делал.
Проверка нужна не выборочная, а сплошная: дыра появляется ровно там, где её не
ждут.

Так и нашлись четыре админских эндпоинта — выдать подписку, отозвать подписку,
забанить и разбанить пользователя. Общая проверка возвращала тройку
`(uid, target_id, err)`, а хендлеры писали `if err: return err`. Но aiohttp
`StreamResponse` определяет `__len__`, поэтому `bool(response)` для ответа с
пустым на тот момент телом равен False: 403 создавался и молча выбрасывался, а
выполнение шло дальше как у администратора. `/admin/user/{id}/unban` отвечал
`{"ok": true}` вообще без токена.

Второй тест запрещает саму форму `if <ответ>:` — чтобы класс не вернулся.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M

ROOT = Path(__file__).resolve().parents[1]

# Роуты, открытые намеренно. Каждый — с причиной, почему токена тут быть не может.
PUBLIC_ROUTES = {
    ("OPTIONS", "/api/miniapp/{path}"): "CORS preflight — до авторизации",
    ("POST", "/api/miniapp/auth"): "сам вход: токен здесь выдаётся",
    ("POST", "/api/miniapp/pair"): "привязка устройства до получения токена",
    ("POST", "/api/miniapp/pair/exchange"): "обмен кода привязки на токен",
    ("GET", "/api/miniapp/sys_health"): "health-проба: только статус БД",
    ("GET", "/api/miniapp/config"): "публичная витрина: имя бота и цены",
}

REFUSAL_STATUSES = (401, 403)


class _Pool:
    async def fetch(self, query, *args): return []
    async def fetchrow(self, query, *args): return None
    async def fetchval(self, query, *args): return None
    async def execute(self, query, *args): return "OK"


class _AnonRequest:
    """Запрос без токена: ровно то, что пришлёт посторонний."""

    def __init__(self, path: str, method: str):
        self.path = path
        self.method = method
        self.match_info = {
            part[1:-1].split(":")[0]: "1"
            for part in path.split("/")
            if part.startswith("{") and part.endswith("}")
        }
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.headers: dict[str, str] = {}

    async def json(self): return {}
    async def post(self): return {}
    async def read(self): return b""


def _routes():
    app = web.Application()
    M.setup_routes(app, _Pool())
    seen = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if not path.startswith("/api/miniapp/"):
            continue
        method = route.method
        if method == "HEAD":          # aiohttp добавляет сам к каждому GET
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        yield method, path, route.handler


def _all_routes():
    return sorted((m, p) for m, p, _ in _routes())


@pytest.fixture
def anonymous(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda request: None)


def test_route_list_is_not_empty():
    """Страховка измерителя: пустой список превратил бы ратчет в заглушку."""
    assert len(_all_routes()) > 500


def test_public_routes_still_exist():
    """Список исключений должен описывать существующие роуты, а не прошлое."""
    live = set(_all_routes())
    stale = [k for k in PUBLIC_ROUTES if k not in live]
    assert not stale, f"в списке публичных роутов остались несуществующие: {stale}"


def test_every_route_refuses_anonymous_caller(anonymous):
    open_routes = []
    for method, path, handler in _routes():
        if (method, path) in PUBLIC_ROUTES:
            continue
        try:
            resp = asyncio.run(handler(_AnonRequest(path, method)))
            status = getattr(resp, "status", None)
        except web.HTTPException as exc:      # отказ исключением — тоже отказ
            status = exc.status
        except Exception as exc:
            # Дошли до тела хендлера и упали там — значит проверки на входе нет.
            open_routes.append(f"{method} {path} → {type(exc).__name__}: {exc}"[:160])
            continue
        if status not in REFUSAL_STATUSES:
            open_routes.append(f"{method} {path} → {status}")
    assert not open_routes, (
        "роуты мини-аппа отвечают постороннему без токена "
        f"({len(open_routes)} шт.):\n  " + "\n  ".join(sorted(open_routes)[:40]))


def test_admin_user_actions_refuse_anonymous(anonymous):
    """Именно эти четыре были открыты — держим их отдельной проверкой."""
    wanted = {
        ("POST", "/api/miniapp/admin/user/{user_id}/grant"),
        ("POST", "/api/miniapp/admin/user/{user_id}/revoke"),
        ("POST", "/api/miniapp/admin/user/{user_id}/ban"),
        ("POST", "/api/miniapp/admin/user/{user_id}/unban"),
    }
    checked = set()
    for method, path, handler in _routes():
        if (method, path) not in wanted:
            continue
        checked.add((method, path))
        resp = asyncio.run(handler(_AnonRequest(path, method)))
        assert resp.status == 403, (
            f"{method} {path} отвечает {resp.status} без токена: "
            f"{resp.body[:120]!r}")
    assert checked == wanted, f"роуты пропали из регистрации: {wanted - checked}"


# ── Статический запрет самой формы ошибки ────────────────────────────────────

RESPONSE_FACTORIES = {"_err", "rate_limit_response", "json_response",
                      "_json_resp", "_csv_resp", "Response"}
# функция → индекс элемента кортежа, который является ответом
TUPLE_RESPONSE = {"_admin_target": 2}


def _python_files():
    for sub in ("services", "bot", "database"):
        d = ROOT / sub
        if d.is_dir():
            yield from (p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    yield from ROOT.glob("*.py")


def test_no_truthiness_check_on_a_response():
    """`if resp:` вместо `if resp is not None:` — молча отключённая проверка."""
    offenders = []
    for path in sorted(_python_files()):
        try:
            src = path.read_text("utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        lines = src.splitlines()
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            names: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                value = node.value.value if isinstance(node.value, ast.Await) else node.value
                if not isinstance(value, ast.Call):
                    continue
                called = (value.func.id if isinstance(value.func, ast.Name)
                          else getattr(value.func, "attr", ""))
                for target in node.targets:
                    if isinstance(target, ast.Name) and called in RESPONSE_FACTORIES:
                        names.add(target.id)
                    elif isinstance(target, ast.Tuple) and called in TUPLE_RESPONSE:
                        idx = TUPLE_RESPONSE[called]
                        if idx < len(target.elts) and isinstance(target.elts[idx], ast.Name):
                            names.add(target.elts[idx].id)
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                test = node.test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    test = test.operand
                if isinstance(test, ast.Name) and test.id in names:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} в {fn.name}(): "
                        f"{lines[node.lineno - 1].strip()}")
    assert not offenders, (
        "aiohttp Response имеет __len__, поэтому bool(ответ) == False при пустом "
        "теле — такая проверка не срабатывает. Нужно `is not None`:\n  "
        + "\n  ".join(sorted(set(offenders))[:20]))
