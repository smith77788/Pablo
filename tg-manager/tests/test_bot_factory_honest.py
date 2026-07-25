"""Регресс: форма «Фабрика ботов» в мини-аппе не обещает того, чего не делает.

Класс 3 (параметр принят, но не доходит до эффекта) + класс 4 (fake success).
AST-свип по хендлерам нашёл: `bot_factory_create` читал `account_id` из тела и
НЕ использовал его ни разу. Разбор показал, что врали сразу три вещи:

  * поле «Аккаунт для BotFather» с подписью «Аккаунт будет писать в @BotFather
    для создания» — на этом пути BotFather НЕ участвует вообще: эндпойнт
    регистрирует УЖЕ существующий токен и применяет оформление через Bot API;
  * поле «Количество ботов» (и подсказка про нумерацию имён) — count принимался,
    возвращался в ответе (создавая иллюзию, что учтён), но создавался ровно ОДИН
    бот — тот, чей токен ввели;
  * тост «✅ Боты создаются. Процесс в фоне» — множественное число и «в фоне»,
    тогда как бот один и создаётся синхронно.

Массовое создание НОВЫХ ботов через BotFather существует — это отдельная операция
op_type="bot_factory" в самом боте; на неё теперь стоит указатель.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"
INDEX = ROOT / "mini_app" / "index.html"


def _handler_src() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def bot_factory_create.*?(?=\n    async def )", src, re.DOTALL)
    assert m, "bot_factory_create не найден"
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


def test_backend_does_not_accept_params_it_ignores():
    h = _handler_src()
    assert 'data.get("account_id")' not in h, (
        "account_id принимался и молча игнорировался — на этом пути BotFather нет"
    )
    assert '"count": count' not in h, (
        "эхо count в ответе создавало иллюзию, что количество учтено"
    )


def _code_only(src: str) -> str:
    """Убрать // -комментарии: пояснение к фиксу не должно ломать проверку кода."""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def test_frontend_sends_only_honored_fields():
    src = _code_only(_fn("submitBotFactory"))
    assert "account_id" not in src, "фронт не должен слать поле, которое игнорируется"
    assert not re.search(r"\bcount\b", src), "count не поддерживается этим путём"
    assert "ecosystem_id" in src, "привязка к экосистеме реально работает — её шлём"


def test_toast_reports_single_bot_not_plural_background():
    src = _fn("submitBotFactory")
    assert "Боты создаются" not in src, (
        "бот один и создаётся синхронно — множественное число и «в фоне» врут"
    )
    assert "applied_settings" in src, "итог должен опираться на реально применённое"


def test_ui_has_no_dead_botfather_and_count_fields():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="bfAccount"' not in html, "селектор «Аккаунт для BotFather» — мёртвый"
    assert 'id="bfCount"' not in html, "поле «Количество ботов» не поддерживается"
    assert "Аккаунт будет писать в @BotFather" not in html, "подпись обещала несуществующее"


def test_ui_points_to_real_mass_factory():
    html = INDEX.read_text(encoding="utf-8")
    assert "Фабрика ботов в самом боте" in html, (
        "пользователю нужен указатель, где реально создаются новые боты пачкой"
    )


def test_no_unused_body_params_in_handler():
    """Общая проверка класса 3 для этого хендлера: каждый прочитанный из тела
    параметр обязан использоваться."""
    tree = ast.parse(API.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "bot_factory_create":
            target = node
            break
    assert target is not None
    assigned = {}
    for n in ast.walk(target):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = n.value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get" and isinstance(v.func.value, ast.Name)
                    and v.func.value.id in ("data", "body")):
                assigned[n.targets[0].id] = True
    used = {n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    dead = sorted(set(assigned) - used)
    assert not dead, f"параметры приняты, но не используются: {dead}"
