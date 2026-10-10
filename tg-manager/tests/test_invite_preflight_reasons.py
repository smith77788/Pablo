"""Пре-флайт объясняет ПОЧЕМУ аккаунты не подключились.

«14 не подключились» без причины — чёрный ящик: оператор не знает, чинить
сессии, менять IP или ждать флуд. run_preflight агрегирует причины неудач в
человеко-понятные корзины, а classify_conn_error маппит сырую ошибку.
"""
from __future__ import annotations

import asyncio

from services.invite_preflight import classify_conn_error, run_preflight


def test_classify_maps_known_causes():
    dup = classify_conn_error("AuthKeyDuplicatedError: ...")
    assert "AUTH_KEY_DUPLICATED" in dup and "двух IP" in dup
    assert "отозвана" in classify_conn_error("AuthKeyUnregisteredError")
    assert "забанен" in classify_conn_error("UserDeactivatedBanError")
    assert "флуд" in classify_conn_error("FloodWaitError: wait 500s").lower()
    assert "таймаут" in classify_conn_error("connect timed out").lower()
    assert "сеть" in classify_conn_error("[Errno 111] Connection refused").lower()
    # неизвестное — обрезанный оригинал, не падаем
    assert classify_conn_error("что-то странное") == "что-то странное"
    assert classify_conn_error("") == "неизвестно"


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return self._rows


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_run_preflight_aggregates_reasons():
    rows = [{"id": i, "phone": f"+{i}", "session_str": "s"} for i in range(1, 5)]

    async def checker(sess, acc, group):
        # 2 конфликта IP, 1 отозвана, 1 участник
        return {
            1: {"state": "no_connect", "error": "AuthKeyDuplicatedError"},
            2: {"state": "no_connect", "error": "AuthKeyDuplicatedError"},
            3: {"state": "no_connect", "error": "AuthKeyUnregisteredError"},
            4: {"state": "member", "can_invite": True},
        }[acc["id"]]

    rep = _run(run_preflight(_FakePool(rows), 1, "@g", account_ids=[1, 2, 3, 4],
                             checker=checker, claim=False))
    assert rep["counts"]["no_connect"] == 3
    assert rep["counts"]["member"] == 1
    reasons = {r["reason"]: r["count"] for r in rep["reasons"]}
    # самая частая причина — первой (конфликт IP ×2)
    assert rep["reasons"][0]["count"] == 2
    dup = next(k for k in reasons if "AUTH_KEY_DUPLICATED" in k)
    assert reasons[dup] == 2
    rev = next(k for k in reasons if "отозвана" in k)
    assert reasons[rev] == 1


def test_run_preflight_claims_free_accounts(monkeypatch):
    """Пре-флайт ЗАХВАТЫВАЕТ свободные аккаунты на время проверки — иначе фоновый
    цикл коннектит ту же сессию параллельно → AUTH_KEY_DUPLICATED (сжигание)."""
    from services import op_worker
    marked: list = []
    released: list = []

    async def fake_try_claim(ids):
        # атомарный захват: аккаунт 2 «занят» реальной операцией → не захватываем
        claimed = [i for i in ids if i != 2]
        marked.extend(claimed)
        return claimed

    async def fake_release(ids):
        released.extend(ids)

    # 3 свободных, аккаунт 2 — «занят» реальной операцией: его НЕ проверяем/захватываем.
    monkeypatch.setattr(op_worker, "try_claim_accounts", fake_try_claim)
    monkeypatch.setattr(op_worker, "release_accounts", fake_release)

    rows = [{"id": i, "phone": f"+{i}", "session_str": "s"} for i in (1, 2, 3)]
    checked_ids: list = []

    async def checker(sess, acc, group):
        checked_ids.append(acc["id"])
        return {"state": "member", "can_invite": True}

    rep = _run(run_preflight(_FakePool(rows), 1, "@g", account_ids=[1, 2, 3],
                             checker=checker, claim=True))
    # захватили и отпустили только свободных (1,3), занятого (2) не трогали
    assert sorted(marked) == [1, 3]
    assert sorted(released) == [1, 3]
    assert 2 not in checked_ids            # занятый не проверялся (без коллизии)
    assert rep["busy"] == 1
    assert rep["total"] == 2
