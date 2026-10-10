"""Клиента Telegram нельзя строить без словаря аккаунта.

`account_manager._make_client(session_string, device)` выбирает ТРАНСПОРТ по
полям второго аргумента: `proxy_url`/`proxy_id` (назначенный прокси),
`cf_relay_url` (персональный Cloudflare-релей), `id` + `ipv6_subnet`
(уникальный IPv6 аккаунта). Забыл словарь — клиент молча уходит НАПРЯМУЮ с
host-IP, хотя остальные подсистемы того же аккаунта в это время идут через его
прокси или релей.

Одна и та же сессия с двух адресов — это AUTH_KEY_DUPLICATED: Telegram
отзывает ключ навсегда, аккаунт мёртв, восстановить его нечем. Это самая
дорогая поломка продукта, и стоит она одной забытой запятой в вызове.

Сам `_make_client` от этого защититься не может: при `device=None` ему нечего
проверять — нет даже `id`, по которому можно было бы добрать транспорт из
карты (`_fill_transport_fields`). Предупреждение в логе есть только для
НЕПОЛНОГО словаря; полностью пропущенный не виден никак. Поэтому инвариант
держится здесь, на разборе исходников.

Так уже ломались `registration_checker._get_telethon_client`,
`entity_analyzer._get_client` и `account_manager.validate_session_import`
(последняя вдобавок принимала `proxy_url` и молча его выбрасывала).

Модуль со СВОИМ `_make_client`, не импортирующий его из `account_manager`
(например `services/ai_claude.py` — фабрика клиента Anthropic), к этому
правилу отношения не имеет и в проверку не попадает.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", "node_modules", "site-packages", "venv", ".venv", "__pycache__"}


def _python_files():
    for path in sorted(ROOT.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        yield path


def _imports_the_factory(tree: ast.AST) -> bool:
    """Модуль тянет `_make_client` именно из account_manager (в т.ч. локальным
    импортом внутри функции — так тут делают почти все)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "account_manager"
        ):
            if any(a.name == "_make_client" for a in node.names):
                return True
    return False


def _module_aliases(tree: ast.AST) -> set[str]:
    """Имена, под которыми в этом файле лежит САМ модуль account_manager.

    Без этого `ai_claude._make_client(30.0)` — чужая фабрика клиента Anthropic —
    считалась бы нарушением: по одному имени атрибута модули не различить.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.endswith("account_manager"):
                    aliases.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "account_manager":
                    aliases.add(a.asname or a.name)
    return aliases


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _is_the_factory_call(node: ast.AST, by_name: bool, aliases: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "_make_client":
        base = _dotted(func.value)
        return base in aliases or base.endswith(".account_manager")
    return by_name and isinstance(func, ast.Name) and func.id == "_make_client"


def _has_account_dict(call: ast.Call) -> bool:
    return len(call.args) >= 2 or any(k.arg == "device" for k in call.keywords)


def _collect():
    total, offenders = 0, []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        if "_make_client" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        by_name = path.name == "account_manager.py" or _imports_the_factory(tree)
        aliases = _module_aliases(tree)
        for node in ast.walk(tree):
            if not _is_the_factory_call(node, by_name, aliases):
                continue
            total += 1
            if not _has_account_dict(node):
                snippet = (ast.get_source_segment(text, node) or "").strip()
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: {snippet[:100]}"
                )
    return total, offenders


def test_every_client_is_built_with_its_account():
    total, offenders = _collect()
    assert not offenders, (
        "клиент Telegram строится без словаря аккаунта — он пойдёт напрямую с "
        "host-IP мимо назначенного прокси/релея, а это AUTH_KEY_DUPLICATED:\n  "
        + "\n  ".join(offenders)
        + "\n\nПередавайте вторым аргументом весь словарь аккаунта из "
        "database.db.telethon_accounts_query() (не только session_str)."
    )


def test_the_detector_actually_looks_at_something():
    """Пустой обход прошёл бы «успешно» и охранял бы пустоту."""
    total, _ = _collect()
    assert total > 50, f"найдено всего {total} вызовов _make_client — сканер сломан"


def test_a_call_without_the_account_is_caught():
    """Проверяем сам измеритель на заведомо дырявом коде."""
    bad = ast.parse(
        "from services.account_manager import _make_client\n"
        "client = _make_client(acc['session_str'])\n"
    )
    calls = [n for n in ast.walk(bad) if _is_the_factory_call(n, True, set())]
    assert len(calls) == 1 and not _has_account_dict(calls[0])


def test_the_detector_accepts_the_right_shapes():
    good = ast.parse(
        "from services.account_manager import _make_client\n"
        "a = _make_client(s, acc)\n"
        "b = _make_client(s, device=acc)\n"
        "from services import account_manager\n"
        "c = account_manager._make_client(s, dict(acc))\n"
    )
    calls = [n for n in ast.walk(good) if _is_the_factory_call(
        n, True, _module_aliases(good))]
    assert len(calls) == 3
    assert all(_has_account_dict(c) for c in calls)


def test_a_foreign_factory_of_the_same_name_is_left_alone():
    """services/ai_claude.py строит клиента Anthropic — правило не про него."""
    other = ast.parse(
        "import services.ai_claude as ai_claude\n"
        "def _make_client(timeout):\n    ...\n"
        "c = _make_client(30)\n"
        "d = ai_claude._make_client(30.0)\n"
    )
    assert not _imports_the_factory(other)
    aliases = _module_aliases(other)
    calls = [n for n in ast.walk(other) if _is_the_factory_call(n, False, aliases)]
    assert calls == [], "чужая фабрика с тем же именем попала под правило"
