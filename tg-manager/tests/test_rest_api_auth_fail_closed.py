"""REST API управления аккаунтами не должен открываться без настроенного ключа.

/api/v1/send_message шлёт сообщение ОТ ИМЕНИ аккаунта владельца,
/api/v1/get_messages читает переписку, /api/v1/accounts отдаёт список
аккаунтов. Проверка ключа начиналась со строки «если ADMIN_SECRET is None —
пускаем всех». Сегодня config подставляет пустую строку, и до этой ветки дело
не доходит, но она остаётся заряженной: достаточно кому-то убрать значение по
умолчанию в config.py, и весь этот API станет публичным, молча.

Не настроен ключ — доступа нет. Это не должно зависеть от того, что именно
вернёт os.getenv.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import rest_api, security


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_no_secret_means_no_access(monkeypatch):
    monkeypatch.setattr("config.ADMIN_SECRET", "", raising=False)
    assert rest_api._check_auth(_Req()) is False
    assert rest_api._check_auth(_Req({"X-Api-Key": "что угодно"})) is False


def test_secret_set_to_none_still_means_no_access(monkeypatch):
    """Именно эта ветка раньше пускала всех."""
    monkeypatch.setattr("config.ADMIN_SECRET", None, raising=False)
    import importlib
    importlib.reload(rest_api)
    try:
        assert rest_api._check_auth(_Req()) is False
        assert rest_api._check_auth(_Req({"X-Api-Key": "x"})) is False
    finally:
        monkeypatch.undo()
        importlib.reload(rest_api)


def test_right_key_is_accepted(monkeypatch):
    monkeypatch.setattr(rest_api, "ADMIN_SECRET", "ключ", raising=False)
    assert rest_api._check_auth(_Req({"X-Api-Key": "ключ"})) is True
    assert rest_api._check_auth(_Req({"Authorization": "Bearer ключ"})) is True
    assert rest_api._check_auth(_Req({"X-Api-Key": "не тот"})) is False


def test_decorator_has_no_open_branch_either():
    src = open(security.__file__, encoding="utf-8").read()
    start = src.index("def require_rest_api_auth(")
    body = src[start:src.index("# ── Combined Security Middleware")]
    assert "return await handler(request)" in body
    assert "if ADMIN_SECRET is None:\n            return await handler(request)" not in body, (
        "ветка «секрет не задан — пускаем всех» вернулась")


def test_every_dangerous_route_checks_auth():
    src = open(rest_api.__file__, encoding="utf-8").read()
    for handler in ("api_accounts", "api_send_message",
                    "api_click_button", "api_get_messages"):
        m = re.search(rf"async def {handler}\(.*?\n(.*?)\n    (async def|app\.router)",
                      src, re.DOTALL)
        assert m, f"{handler} не найден"
        assert "_check_auth(request)" in m.group(1), f"{handler} без проверки ключа"
