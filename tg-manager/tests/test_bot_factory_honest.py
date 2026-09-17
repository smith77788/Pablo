"""Фабрика ботов в мини-аппе: два пути, оба делают ровно то, что обещают.

ИСТОРИЯ (важна, чтобы не откатить назад):
AST-свип класса 3 нашёл, что `bot_factory_create` читал `account_id` и не
использовал его. Разбор вскрыл тройной обман формы: поле «Аккаунт для BotFather»
(BotFather на том пути не участвует), поле «Количество ботов» (создавался ровно
один — тот, чей токен ввели, а count ещё и возвращался в ответе) и тост
«Боты создаются. Процесс в фоне» при одном боте синхронно.

Первым решением поля были УБРАНЫ, а пользователь отправлен в бот. Продуктовое
требование это отменило: мини-апп обязан уметь всё, что умеет бот, — включая
РЕАЛЬНОЕ создание ботов. Поэтому теперь путей два, и инвариант другой:
поля существуют И подключены к настоящей операции.

  * режим 'new'   → POST /bot_factory/create_new → operation_bus.submit(
                    "bot_factory") — аккаунт реально пишет @BotFather, фон;
  * режим 'token' → POST /bot_factory/create — регистрация готового токена,
                    синхронно, без BotFather (и без параметров, которые он бы
                    проигнорировал).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"
INDEX = ROOT / "mini_app" / "index.html"


def _handler(name: str) -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def " + name + r"\b.*?(?=\n    async def )", src, re.DOTALL)
    assert m, f"{name} не найден"
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


def _code_only(src: str) -> str:
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


# ── путь 1: РЕАЛЬНОЕ создание через BotFather (паритет с ботом) ──────────────

def test_real_factory_endpoint_submits_operation():
    h = _handler("bot_factory_create_new")
    assert 'operation_bus.submit' in h and '"bot_factory"' in h, (
        "мини-апп обязан уметь реально создавать ботов, а не только регистрировать токен"
    )
    for key in ("acc_id", "count", "name_template", "uname_template"):
        assert key in h, f"контракт операции должен совпадать с ботовым: нет {key}"
    assert "total_items=count" in h, "прогресс операции должен считаться по числу ботов"


def test_real_factory_validates_account_before_queueing():
    """Иначе операция молча встанет в очередь и провалится уже в воркере."""
    h = _handler("bot_factory_create_new")
    assert "FROM tg_accounts WHERE id=$1 AND owner_id=$2" in h, "аккаунт скоупим по владельцу"
    assert "is_active" in h and "session_str" in h, "аккаунт без сессии писать BotFather не сможет"


def test_plan_refusal_is_403_not_500():
    h = _handler("bot_factory_create_new")
    assert "except PermissionError" in h and "403" in h, (
        "отказ шины по тарифу — честный 403, а не сырой 500"
    )


def test_frontend_new_mode_sends_real_params():
    src = _code_only(_fn("submitBotFactory"))
    assert "bot_factory/create_new" in src, "режим «создать новых» должен звать реальный эндпойнт"
    assert "account_ids" in src and "name_template" in src, (
        "мультивыбор аккаунтов и шаблон имени обязаны уходить в реальную операцию")
    assert ".ca-acc-cb" in src, "аккаунты берутся из чекбоксов мультипикера"
    assert "askConfirm" in src, "создание ботов необратимо — нужно подтверждение"
    assert "pollOpResult" in src, "фоновая операция должна показывать пользователю итог"


def test_endpoint_accepts_multi_account():
    """Реальный эндпойнт умеет создавать ботов СРАЗУ на нескольких аккаунтах."""
    h = _handler("bot_factory_create_new")
    for key in ("account_ids", "bot_count", "bot_name", "base_username"):
        assert key in h, f"multi-контракт исполнителя требует {key}"


def test_ui_has_both_modes_and_real_fields():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="bfAccts"' in html, "мультивыбор аккаунтов для @BotFather — часть реального пути"
    assert 'id="bfCount"' in html, "количество ботов реально поддерживается операцией"
    assert 'bfSetMode(' in html, "нужен явный переключатель режимов, а не одна лгущая форма"


# ── путь 2: регистрация готового токена (без BotFather) ─────────────────────

def test_token_path_does_not_accept_ignored_params():
    h = _handler("bot_factory_create")
    assert 'data.get("account_id")' not in h, "на этом пути BotFather нет — account_id лишний"
    assert '"count": count' not in h, "эхо count создавало иллюзию, что количество учтено"


def test_token_path_toast_is_honest():
    src = _code_only(_fn("submitBotFactory"))
    assert "Боты создаются" not in src, "по токену регистрируется ОДИН бот, синхронно"
    assert "applied_settings" in src, "итог должен опираться на реально применённое"


# ── общий инвариант класса 3 ────────────────────────────────────────────────

def test_no_unused_body_params_in_both_handlers():
    tree = ast.parse(API.read_text(encoding="utf-8"))
    for fname in ("bot_factory_create", "bot_factory_create_new"):
        target = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == fname), None)
        assert target is not None, f"{fname} не найден"
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
        assert not dead, f"{fname}: параметры приняты, но не используются: {dead}"
