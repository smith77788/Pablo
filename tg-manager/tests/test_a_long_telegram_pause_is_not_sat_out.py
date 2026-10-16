"""Длинную паузу Telegram прогон не пересиживает в слоте.

Паузу, назначенную Telegram, прогон имеет право пересидеть только до
`_FLOOD_INLINE_MAX_S` — этот потолок согласован со сторожем застоя
(`_inline_pause_cap`), и для всего, что длиннее, в воркере есть свои пути
(`_flood_sleep`, `_defer_op_for_flood`, `_pause_watching_for_cancel`).

Массовое вступление и массовый выход пересиживали флуд сырым `asyncio.sleep` на
всю назначенную длину — а FloodWait на вступлении легко бывает часовым. Цена:

  • отмена владельца не замечалась: она доходит до исполнителя опросом на витке
    цикла, а виток стоял в sleep — «Отменить» нажато, операция ещё час показана
    работающей;
  • слот очереди и арендованные аккаунты держались всё это время, то есть
    остальные операции владельца стояли из-за одного оштрафованного аккаунта;
  • `done_items` при этом не двигался, и сторож застоя снимал прогон за
    соблюдение собственного анти-бан темпа — с расходом бюджета живучести.

Укоротить саму паузу нельзя: штраф назначил Telegram, а цена ошибки в этом
классе — мёртвый аккаунт. Поэтому у аккаунта заканчиваются остальные цели
(штраф записан в flood_engine, пейсинг и карантин его учтут), а остальной флот
работает дальше — ровно как в ветке PEER_FLOOD по соседству.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return 0

    async def execute(self, query, *args):
        self.queries.append(query)
        return "UPDATE 1"


class _LongFlood(Exception):
    """FloodWait на час: именно так его отдаёт Telethon (атрибут .seconds)."""

    seconds = 3600


@pytest.fixture
def _spy(monkeypatch):
    """Сны не спим, а записываем: нас интересует, НА СКОЛЬКО прогон встал."""
    from services import op_worker

    slept: list[float] = []

    async def _instant(sec=0, *a, **kw):
        slept.append(float(sec or 0))

    monkeypatch.setattr(asyncio, "sleep", _instant)
    monkeypatch.setattr(op_worker, "_governed_delay", lambda pool, owner, base: _zero(base))

    async def _not_quarantined(pool, acc_id):
        return False

    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    return slept


async def _zero(base=0.0):
    return float(base)


@pytest.mark.asyncio
async def test_bulk_join_stops_the_account_instead_of_sitting_on_the_flood(_spy, monkeypatch):
    from services import account_manager, op_worker

    attempted: list[str] = []

    async def _join(session_str, link, _acc=None):
        attempted.append(link)
        raise _LongFlood("A wait of 3600 seconds is required")

    monkeypatch.setattr(account_manager, "join_channel", _join)

    res = await op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555,
        {"links": ["t.me/a", "t.me/b", "t.me/c"], "delay_mode": "fast"},
        [{"id": 1, "phone": "+70000000001", "session_str": "s1"}],
    )

    assert attempted == ["t.me/a"], (
        "после часовой паузы Telegram аккаунт пошёл по остальным ссылкам — "
        f"значит прогон её пересидел: {attempted}")
    assert max(_spy or [0]) <= op_worker._FLOOD_INLINE_MAX_S, (
        f"прогон встал на {max(_spy):.0f}с, потолок — {op_worker._FLOOD_INLINE_MAX_S}с: "
        "отмена не дойдёт, слот и аккаунты заняты, сторож застоя снимет прогон")
    assert res["failed"] >= 1, res


@pytest.mark.asyncio
async def test_bulk_leave_stops_the_account_instead_of_sitting_on_the_flood(_spy, monkeypatch):
    from services import account_manager, op_worker, resource_selector

    attempted: list[str] = []

    async def _leave(session_str, channel, _acc=None, **kw):
        attempted.append(str(channel))
        raise _LongFlood("A wait of 3600 seconds is required")

    async def _select(pool, owner_id, **kw):
        return [{"id": 1, "phone": "+70000000001", "session_str": "s1"}]

    monkeypatch.setattr(account_manager, "leave_channel", _leave)
    monkeypatch.setattr(resource_selector, "select_all_active", _select)
    monkeypatch.setattr(op_worker, "_claim_available_accounts",
                        lambda op_id, accounts, owner_id=None: _same(accounts))
    monkeypatch.setattr(op_worker, "release_accounts", lambda ids: _none())
    monkeypatch.setattr(op_worker, "_budget_remaining",
                        lambda pool, ids: _dict({int(i): 100 for i in ids}))

    res = await op_worker._exec_bulk_leave(
        _Pool(), object(), 78, 555,
        {"channels": ["@one", "@two", "@three"], "delay_mode": "fast"},
    )

    assert len(attempted) == 1, (
        "после часовой паузы Telegram аккаунт пошёл по остальным каналам — "
        f"значит прогон её пересидел: {attempted}")
    assert max(_spy or [0]) <= op_worker._FLOOD_INLINE_MAX_S, (
        f"прогон встал на {max(_spy):.0f}с, потолок — {op_worker._FLOOD_INLINE_MAX_S}с")
    assert res["failed"] >= 1, res


async def _same(accounts):
    return list(accounts)


async def _none():
    return None


async def _dict(d):
    return d
