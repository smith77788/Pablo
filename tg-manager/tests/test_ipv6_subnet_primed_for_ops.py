"""IPv6-подсеть владельца обязана доезжать до процесса, который ведёт операции.

ЧЕМ ЭТО ОПАСНО. `_make_client` выбирает транспорт по подсети владельца, а берёт
её из кэша `account_manager._OWNER_IPV6_SUBNET`. Кэш заполняется ровно в одном
месте — при СОХРАНЕНИИ настройки, то есть в процессе, обслужившем запрос
мини-аппа. Процесс-воркер (INFRAGRAM_ROLE=worker) настройку никогда не видел: у
него словарь пуст, аккаунт уходит НАПРЯМУЮ с host-IP. Одиночные вызовы при этом
читают подсеть из БД и идут с IPv6 аккаунта.

Одна сессия с двух адресов — это AUTH_KEY_DUPLICATED: Telegram убивает auth-key
НАВСЕГДА. Снаружи это выглядит как «аккаунт подключён, но операция ничего не
делает» — то есть как поломка продукта, а не как настройка транспорта.

Рядом уже стоит праймер политики прокси (`set_owner_proxy_policy`) — ровно по
той же причине. Здесь закрывается вторая половина.
"""
from __future__ import annotations

import ast
import asyncio
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _func_src(rel: str, name: str) -> str:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {rel}")


def test_operation_start_primes_ipv6_subnet_next_to_proxy_policy():
    """Праймер подсети стоит там же, где праймер политики — в старте операции."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = src.index("set_owner_proxy_policy(owner_id")
    window = src[i:i + 1200]
    assert "set_owner_ipv6_subnet(" in window, (
        "политика прокси праймится, а IPv6-подсеть нет — массовая операция уведёт "
        "аккаунт напрямую, пока одиночные вызовы идут с его IPv6 (AUTH_KEY_DUPLICATED)")
    assert "get_ipv6_subnet_or_raise" in window, (
        "мягкое чтение возвращает '' и при сбое БД — это ЗАТРЁТ верный кэш и само "
        "уведёт аккаунт напрямую; здесь нужен вариант, пробрасывающий ошибку")


# ── контракт двух читателей подсети ──────────────────────────────────────────

class _Pool:
    def __init__(self, value=None, fail=False):
        self.value, self.fail = value, fail

    async def fetchval(self, *_a, **_k):
        if self.fail:
            raise RuntimeError("БД недоступна")
        return self.value


def test_strict_reader_distinguishes_missing_from_unreadable():
    from database import db

    assert _run(db.get_ipv6_subnet_or_raise(_Pool(None), 7)) == "", "не задана → ''"
    assert _run(db.get_ipv6_subnet_or_raise(
        _Pool({"ipv6_subnet": "2001:db8::/64"}), 7)) == "2001:db8::/64"

    try:
        _run(db.get_ipv6_subnet_or_raise(_Pool(fail=True), 7))
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "сбой чтения выдан за «подсети нет» — именно так аккаунт и уходит "
            "напрямую с host-IP при живой настройке IPv6")


def test_soft_reader_keeps_its_old_contract():
    """Мягкий вариант остался мягким: старые вызовы не должны начать падать."""
    from database import db

    assert _run(db.get_ipv6_subnet(_Pool(fail=True), 7)) == ""
    assert _run(db.get_ipv6_subnet(_Pool(None), 7)) == ""
    assert _run(db.get_ipv6_subnet(_Pool('{"ipv6_subnet": "2001:db8::/64"}'), 7)) \
        == "2001:db8::/64", "строковый JSON тоже обязан читаться"
    assert _run(db.get_ipv6_subnet(_Pool("{битый"), 7)) == "", "битый JSON — не падение"
    assert _run(db.get_ipv6_subnet(_Pool("x"), None)) == "", "без владельца — ''"


def test_priming_a_subnet_changes_the_transport_choice():
    """Смысл праймера: с подсетью в кэше клиент строится на IPv6, без неё — прямо.

    Проверяем именно РЕШЕНИЕ транспорта, а не факт записи в словарь: словарь —
    деталь, а разъехавшийся транспорт — мёртвая сессия.
    """
    import pytest
    pytest.importorskip("telethon", reason="сборка клиента тянет telethon")
    from services import account_manager as am

    acc = {"id": 4242, "owner_id": 909090, "session_str": "", "proxy_id": None,
           "proxy_url": "", "cf_relay_url": ""}
    saved = am._OWNER_IPV6_SUBNET.get(909090)
    try:
        am._OWNER_IPV6_SUBNET.pop(909090, None)          # как в воркере без праймера
        c1 = am._make_client("", dict(acc))
        assert getattr(c1, "_infragram_transport", None) == "direct", (
            "без подсети ожидается прямой host-IP")

        am.set_owner_ipv6_subnet(909090, "2001:db8::/64")  # праймер отработал
        c2 = am._make_client("", dict(acc))
        assert getattr(c2, "_infragram_transport", None) == "ipv6", (
            "подсеть известна, а транспорт всё равно прямой — праймер бесполезен")
    finally:
        if saved is None:
            am._OWNER_IPV6_SUBNET.pop(909090, None)
        else:
            am._OWNER_IPV6_SUBNET[909090] = saved
