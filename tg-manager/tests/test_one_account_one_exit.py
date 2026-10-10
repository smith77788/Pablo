"""Регрессия: один аккаунт — один исходящий адрес, из какой бы подсистемы ни шли.

`_make_client` выбирает выход по полям словаря аккаунта. Кроме прокси и релея
на это влияет `owner_id`: по нему берутся политика прокси владельца и его
IPv6-подсеть. В коде больше сотни собственных SELECT-ов к tg_accounts, и многие
owner_id не берут.

Страховка `_ACC_TRANSPORT` эту дыру не закрывала дважды:

1. карта добирала только `cf_relay_url`, `proxy_id`, `proxy_url`, но не
   `owner_id`;
2. в карту попадали ТОЛЬКО аккаунты с релеем или прокси — то есть ровно не те,
   чей выход зависит от owner_id.

Плюс ветка IPv6 читала исходный `device`, а не словарь после добора, поэтому
добор до неё не доезжал.

Итог для продукта: одна сессия выходила с двух адресов — AUTH_KEY_DUPLICATED,
Telegram отзывает ключ навсегда.
"""
from __future__ import annotations

import ast
import os

import pytest

from services import account_manager as am

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_map():
    saved = dict(am._ACC_TRANSPORT)
    am._ACC_TRANSPORT.clear()
    yield
    am._ACC_TRANSPORT.clear()
    am._ACC_TRANSPORT.update(saved)


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, sql, *args):
        """Ведёт себя как БД: уважает WHERE, иначе проверка ничего не проверяет."""
        self.queries.append(sql)
        rows = list(self.rows)
        if "cf_relay_url IS NOT NULL" in sql:
            rows = [r for r in rows if r["cf_relay_url"] or r["proxy_id"]]
        if args:
            rows = [r for r in rows if r["owner_id"] == args[0]]
        return rows


def _row(acc_id, owner_id, relay=None, proxy_id=None, proxy_url=None):
    return {"id": acc_id, "owner_id": owner_id, "cf_relay_url": relay,
            "proxy_id": proxy_id, "proxy_url": proxy_url}


# --- карта транспорта знает и аккаунты с прямым выходом -------------------

def test_direct_accounts_are_in_the_transport_map():
    """Аккаунт без релея и прокси обязан попасть в карту: его выход решает owner_id."""
    import asyncio

    pool = _Pool([_row(1, 500), _row(2, 500, relay="https://relay.example")])
    n = asyncio.run(am.prime_account_transport(pool))

    assert n == 2, f"в карте {n} аккаунтов вместо двух — прямой выход снова без страховки"
    assert am._ACC_TRANSPORT[1]["owner_id"] == 500


def test_transport_map_remembers_the_owner():
    import asyncio

    pool = _Pool([_row(7, 900)])
    asyncio.run(am.prime_account_transport(pool))
    assert am._ACC_TRANSPORT[7].get("owner_id") == 900


# --- добор owner_id -------------------------------------------------------

def test_owner_id_is_filled_from_the_map():
    am.set_account_transport(11, owner_id=77, cf_relay_url="", proxy_id=None)
    device = {"id": 11}
    filled = am._fill_transport_fields(device)

    assert "owner_id" in filled, f"owner_id не добран: {filled!r}"
    assert device["owner_id"] == 77


def test_explicit_owner_id_wins_over_the_map():
    """Явное значение всегда сильнее кэша — иначе устаревшая запись уводит не туда."""
    am.set_account_transport(11, owner_id=77)
    device = {"id": 11, "owner_id": 5}
    am._fill_transport_fields(device)
    assert device["owner_id"] == 5


def test_owner_policy_applies_after_the_fill():
    """Политика владельца должна сработать на словаре без owner_id."""
    am.set_account_transport(12, owner_id=88)
    am.set_owner_proxy_policy(88, "strict")
    try:
        device = {"id": 12}
        am._fill_transport_fields(device)
        assert am._effective_proxy_policy(device) == "strict", (
            "политика владельца не применилась — аккаунт пойдёт другим выходом"
        )
    finally:
        am.set_owner_proxy_policy(88, None)


# --- ветка IPv6 читает словарь ПОСЛЕ добора -------------------------------

def _fn_body(src: str, name: str) -> str:
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def test_ipv6_branch_reads_the_filled_dict():
    src = open(os.path.join(_ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    body = _fn_body(src, "_make_client")
    assert 'device.get("ipv6_subnet")' not in body, (
        "ветка IPv6 читает исходный словарь — добор транспорта до неё не доедет"
    )
    assert 'device.get("owner_id")' not in body, (
        "ветка IPv6 берёт owner_id из исходного словаря, минуя добор"
    )
    assert 'd.get("ipv6_subnet")' in body and 'd.get("owner_id")' in body


def test_missing_owner_id_is_noticed():
    src = open(os.path.join(_ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    body = _fn_body(src, "_make_client")
    assert '"cf_relay_url", "proxy_id", "owner_id"' in body, (
        "словарь с релеем, но без владельца, снова не проверяется"
    )
