"""Регресс: массовое создание каналов доступно из мини-аппа (паритет с ботом).

Экран «Фабрика каналов» умел создавать РОВНО ОДИН канал, тогда как бот давно
ставит операцию `bulk_create_channels` пачкой. По продуктовому требованию
мини-апп обязан уметь всё то же самое.

Контракт параметров повторяет ботовый путь один-в-один (prefix/count/about/
username_pattern/acc_id, total_items=count) — второй источник правды не плодим.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"
INDEX = ROOT / "mini_app" / "index.html"


def _handler() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def channels_bulk_create\b.*?(?=\n    async def )", src, re.DOTALL)
    assert m, "channels_bulk_create не найден"
    return m.group(0)


def _fn(name: str) -> str:
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"(?:async\s+)?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", html)
    assert m, f"{name} не найдена"
    i, depth = m.end() - 1, 0
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[m.start(): i + 1]
        i += 1
    raise AssertionError("тело не закрыто")


def test_endpoint_submits_bulk_operation():
    h = _handler()
    assert 'operation_bus.submit' in h and '"bulk_create_channels"' in h
    for key in ("prefix", "count", "about", "username_pattern", "acc_id"):
        assert key in h, f"контракт должен совпадать с ботовым: нет {key}"
    assert "total_items=count" in h, "прогресс считается по числу каналов"


def test_route_registered():
    src = API.read_text(encoding="utf-8")
    assert '"/api/miniapp/channels/bulk_create"' in src, "роут не зарегистрирован"


def test_account_validated_before_queueing():
    h = _handler()
    assert "FROM tg_accounts WHERE id=$1 AND owner_id=$2" in h, "owner-скоуп обязателен"
    assert "is_active" in h and "session_str" in h, (
        "аккаунт без сессии не создаст канал — операция провалилась бы в воркере"
    )


def test_plan_refusal_is_403():
    h = _handler()
    assert "except PermissionError" in h and "403" in h


def test_count_is_bounded():
    h = _handler()
    assert "max_val=20" in h, "верхняя граница защищает от случайной пачки в сотни каналов"


def test_frontend_switches_to_bulk_and_confirms():
    src = _fn("submitChannelFactory")
    assert "channels/bulk_create" in src, "при count>1 должен вызываться массовый эндпойнт"
    assert "count > 1" in src, "одиночный путь остаётся для одного канала"
    assert "askConfirm" in src, "создание каналов необратимо — нужно подтверждение"
    assert "pollOpResult" in src, "фоновая операция должна доводить итог до пользователя"


def test_ui_exposes_count_and_username_pattern():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="cfCount"' in html and 'id="cfUsernamePattern"' in html


def test_no_unused_body_params():
    tree = ast.parse(API.read_text(encoding="utf-8"))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "channels_bulk_create"), None)
    assert target is not None
    assigned = set()
    for n in ast.walk(target):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = n.value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get" and isinstance(v.func.value, ast.Name)
                    and v.func.value.id in ("data", "body")):
                assigned.add(n.targets[0].id)
    used = {n.id for n in ast.walk(target)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    dead = sorted(assigned - used)
    assert not dead, f"параметры приняты, но не используются: {dead}"
