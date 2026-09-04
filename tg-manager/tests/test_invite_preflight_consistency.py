"""Пре-флайт обязан обещать ровно то, что сделает прогон.

ЧТО БЫЛО СЛОМАНО. count_already_invited сравнивал цели сырым
`target = ANY($3::text[])` — регистрозависимо, тогда как дедуп исполнителя
сравнивает регистронезависимо. Для username'ов, чей регистр в списке отличался
от записанного в журнал, пре-флайт ЗАНИЖАЛ «уже приглашено» и обещал оператору
больше новых целей, чем операция потом брала. Цифра, расходящаяся с фактом,
хуже, чем её отсутствие: по ней принимают решение о запуске.
"""
from __future__ import annotations

import asyncio

from services import invite_preflight as pf
from services.contact_opt_out import compare_key


class _Pool:
    """Журнал инвайтов, отвечающий по тому же правилу, что и Postgres lower()."""

    def __init__(self, logged):
        self.logged = {str(t).lower() for t in logged}
        self.asked = None

    async def fetchval(self, q, *a):
        assert "lower(target)" in q, "сравнение должно быть регистронезависимым"
        self.asked = a[2]
        return sum(1 for k in a[2] if k in self.logged)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_counts_ignore_username_case():
    pool = _Pool(["@ivan", "@petr"])
    n = _run(pf.count_already_invited(pool, 1, "@chat", ["@Ivan", "@PETR", "@new"]))
    assert n == 2, "регистр username не должен менять число «уже приглашено»"


def test_keys_match_the_executor_rule():
    """Ключи, которые уходят в запрос, — те же, что строит дедуп исполнителя."""
    pool = _Pool([])
    _run(pf.count_already_invited(pool, 1, "@chat", ["@Ivan", "123", "+79990000001"]))
    assert set(pool.asked) == {compare_key(x) for x in ("@Ivan", "123", "+79990000001")}


def test_duplicates_in_input_do_not_inflate_the_count():
    pool = _Pool(["@ivan"])
    n = _run(pf.count_already_invited(pool, 1, "@chat", ["@Ivan", "@ivan", "@IVAN"]))
    assert n == 1, "одна и та же цель в списке не должна считаться трижды"


def test_fail_open_on_empty_and_oversized_input():
    pool = _Pool(["@ivan"])
    assert _run(pf.count_already_invited(pool, 1, "@chat", [])) == 0
    assert _run(pf.count_already_invited(pool, 1, "", ["@ivan"])) == 0
    assert _run(pf.count_already_invited(pool, 1, "@chat", ["x"] * 10, cap=5)) == 0


def test_compare_key_is_a_pure_lowercase_rule():
    """Правило одно и то же на обеих сторонах: Python и SQL lower()."""
    for v in ("@Ivan", "123456789", "+79991234567", "@MiXeD_Case"):
        assert compare_key(v) == str(v).lower()
