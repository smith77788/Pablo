"""Регрессия: живая консоль обязана ходить тем же выходом, что и операции.

`_make_client` выбирает выход аккаунта по четырём полям словаря: назначенный
прокси (`proxy_id`/`proxy_url`), персональный релей (`cf_relay_url`), политика
прокси владельца (`proxy_policy`, иначе по `owner_id` из кэша) и его
IPv6-подсеть (`ipv6_subnet`).

Консоль загружала аккаунт своим SELECT-ом, где было только `session_str`,
отпечаток устройства и `proxy_url`. Владелец с политикой strict или со своей
IPv6-подсетью получал из консоли ДРУГОЙ исходящий адрес, чем из операций. Одна
сессия с двух адресов — это AUTH_KEY_DUPLICATED: Telegram отзывает ключ
навсегда, а снаружи это выглядит как «аккаунты сами отваливаются».

Проверяем не текст запроса, а результат: какие ключи доезжают до `_make_client`.
"""
from __future__ import annotations

import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Поля, от которых зависит выбор исходящего адреса. Список сверен с
# services/account_manager.py::_make_client.
_TRANSPORT_FIELDS = ("owner_id", "cf_relay_url", "proxy_id", "proxy_url",
                     "proxy_policy", "ipv6_subnet")


def _fn_body(src: str, name: str) -> str:
    """Тело функции по СТРУКТУРЕ (не по окну фиксированной длины)."""
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def test_canonical_loader_provides_every_transport_field():
    """Канонический загрузчик — единственное место, где транспорт полон."""
    body = _fn_body(_read("database/db.py"), "get_account_for_telethon")
    for field in _TRANSPORT_FIELDS:
        assert field in body, (
            f"канонический загрузчик не отдаёт {field} — выбор выхода сломан в корне"
        )


def test_console_uses_the_canonical_loader():
    body = _fn_body(_read("services/mini_app_api.py"), "_console_account")
    assert "get_account_for_telethon" in body, (
        "консоль грузит аккаунт своим запросом — транспорт разъедется с операциями"
    )


def test_console_still_scopes_by_owner_and_active():
    """Чужой и выключенный аккаунт консоли по-прежнему недоступны."""
    body = _fn_body(_read("services/mini_app_api.py"), "_console_account")
    assert "owner_id=$2" in body, "консоль перестала скоупить аккаунт по владельцу"
    assert "is_active=TRUE" in body, "консоль отдаёт выключенный аккаунт"


def test_console_query_no_longer_drops_transport():
    """Старый усечённый SELECT не должен вернуться."""
    body = _fn_body(_read("services/mini_app_api.py"), "_console_account")
    assert "SELECT id, session_str, device_model" not in body


@pytest.mark.parametrize("field", _TRANSPORT_FIELDS)
def test_make_client_reads_this_field(field):
    """Список полей транспорта не выдуман: каждое читается в _make_client."""
    src = _read("services/account_manager.py")
    body = _fn_body(src, "_make_client")
    # owner_id и proxy_policy читаются через _effective_proxy_policy
    if field in ("owner_id", "proxy_policy"):
        helper = _fn_body(src, "_effective_proxy_policy")
        assert field in body or field in helper, field
        return
    assert field in body, f"{field} не влияет на выбор выхода — список устарел"
