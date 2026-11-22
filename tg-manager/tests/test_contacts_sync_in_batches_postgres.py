"""Синк контактов пишет пачками: число запросов не растёт с адресной книгой.

`sync_account` делал три запроса на человека (найти, записать, привязать
источник). Стенд, 10 000 контактов у аккаунта: 8,6 с первый синк и 9,8 с
повторный -> 0,8 и 0,6 с. Проверяем на живой базе: запросов столько же на 3
контакта, сколько на 300; созданные и обновлённые те же; повторный синк
не плодит дублей и привязок.
"""
from __future__ import annotations

from tests.test_contacts_sync_e2e_postgres import (  # noqa: F401
    OWNER, _run, _seed_account, _stub_contacts, pool,
)


class _Counting:
    def __init__(self, pool):
        self._p, self.calls = pool, 0

    def __getattr__(self, name):
        real = getattr(self._p, name)
        if name not in ("fetch", "fetchrow", "fetchval", "execute", "executemany"):
            return real

        async def _wrapped(*a, **kw):
            self.calls += 1
            return await real(*a, **kw)
        return _wrapped


def _book(n, start=0):
    return [{"user_id": 7_100_000 + i, "username": f"bk{i}", "phone": f"+7911{i:07d}",
             "first_name": "Имя", "last_name": str(i), "is_mutual": True}
            for i in range(start, start + n)]


def test_query_count_does_not_grow_with_the_book(pool, monkeypatch):
    from services.contacts_hub import sync_service

    calls = []
    for n in (3, 300):
        acc_id = _run(_seed_account(pool))
        _stub_contacts(monkeypatch, book=_book(n), dialogs=[])
        cp = _Counting(pool)
        res = _run(sync_service.sync_account(cp, OWNER, acc_id))
        assert res["created"] == n, res
        calls.append(cp.calls)
    assert calls[0] == calls[1], f"запрос на контакт: {calls[0]} на 3, {calls[1]} на 300"


def test_resync_updates_without_twins(pool, monkeypatch):
    from services.contacts_hub import sync_service

    acc_id = _run(_seed_account(pool))
    _stub_contacts(monkeypatch, book=_book(50), dialogs=[])
    _run(sync_service.sync_account(pool, OWNER, acc_id))
    _stub_contacts(monkeypatch, book=_book(60), dialogs=[])  # 50 знакомых + 10 новых
    res = _run(sync_service.sync_account(pool, OWNER, acc_id))
    assert (res["created"], res["updated"]) == (10, 50), res
    n, links = _run(pool.fetchrow(
        "SELECT (SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1) AS n, "
        "(SELECT COUNT(*) FROM contact_sources cs JOIN unified_contacts u ON u.id=cs.contact_id "
        " WHERE u.owner_id=$1) AS links", OWNER))
    assert (n, links) == (60, 60)
