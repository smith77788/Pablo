"""Проверка прокси: итоги пишутся одним запросом и не теряются при обрыве.

Раньше на каждый прокси шёл свой UPDATE — до двухсот обращений к базе за одну
проверку. Свели в один запрос, но тогда появился второй риск: если запрос
владельца прервали на середине (закрыл приложение, шлюз отвалился), то всё
проверенное пропало бы. Поэтому запись стоит в finally — проверенное
сохраняется, даже когда проверка не доехала до конца.
"""
from __future__ import annotations

import asyncio


from services import mini_app_api as M


class _Pool:
    """Заглушка пула: помнит запросы и умеет отдавать список прокси."""

    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, q, *args):
        self.queries.append(" ".join(q.split()))
        if "FROM user_proxies" in q and "SELECT" in q.upper():
            return self.rows
        # батч-UPDATE ... RETURNING p.id
        if q.upper().lstrip().startswith("UPDATE"):
            return [{"id": pid} for pid in (args[0] or [])]
        return []

    async def execute(self, q, *args):
        self.queries.append(" ".join(q.split()))
        return "UPDATE 1"

    async def fetchval(self, q, *args):
        self.queries.append(" ".join(q.split()))
        return 0

    async def fetchrow(self, q, *args):
        self.queries.append(" ".join(q.split()))
        return None


def _rows(n):
    return [{"id": 100 + i, "proxy_url": f"socks5://u:p@10.0.0.{i}:1080"}
            for i in range(n)]


def _updates(pool):
    return [q for q in pool.queries if q.upper().startswith("UPDATE USER_PROXIES")]


def test_all_verdicts_go_in_one_update(monkeypatch):
    pool = _Pool(_rows(40))

    async def _probe(url):
        # Живым считаем каждый второй: адрес хоста оканчивается на чётную цифру.
        host = url.split("@", 1)[1].split(":", 1)[0]
        return {"ok": int(host.rsplit(".", 1)[1]) % 2 == 0}

    monkeypatch.setattr("services.proxy_selector.probe_proxy", _probe)
    res = asyncio.run(M._check_all_proxies_core(pool, 777))

    assert res["checked"] == 40
    assert res["alive"] == 20
    assert len(_updates(pool)) == 1, f"UPDATE должен быть один: {_updates(pool)}"


def test_partial_verdicts_survive_a_broken_run(monkeypatch):
    """Часть проверок сорвалась — записано то, что успели проверить."""
    pool = _Pool(_rows(20))
    probed = {"n": 0}

    async def _probe(url):
        probed["n"] += 1
        if probed["n"] > 5:
            raise asyncio.CancelledError
        return {"ok": True}

    monkeypatch.setattr("services.proxy_selector.probe_proxy", _probe)
    res = asyncio.run(M._check_all_proxies_core(pool, 777))

    # Пять успевших проверок живы и записаны, остальные не выдуманы.
    assert res["alive"] == 5
    ups = _updates(pool)
    assert len(ups) == 1, f"итоги не записаны при обрыве: {pool.queries}"


def test_nothing_probed_means_no_query_at_all():
    """Пустой словарь итогов не должен идти в базу впустую."""
    pool = _Pool([])
    assert asyncio.run(M._persist_proxy_verdicts(pool, 777, {})) == 0
    assert pool.queries == []


def test_owner_scope_is_in_the_update():
    pool = _Pool([])
    asyncio.run(M._persist_proxy_verdicts(pool, 777, {1: True, 2: False}))
    assert "p.owner_id = $3" in _updates(pool)[0], (
        "пропал скоуп по владельцу — можно погасить чужой прокси")
