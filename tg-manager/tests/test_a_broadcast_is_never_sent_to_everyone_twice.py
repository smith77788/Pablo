"""Сбой базы не должен превращать докатку рассылки в повторную рассылку.

Докатка оборвавшихся рассылок (`resume_interrupted`) запускается на КАЖДОМ
рестарте процесса, то есть на каждом деплое рабочей ветки. Чтобы не писать
людям второй раз, она читает список уже доставленных и пропускает их.

Это чтение фейлилось ОТКРЫТО: любая ошибка базы давала пустой список с
подписью «starting fresh». Пустой список здесь означает «никому ещё не
отправляли» — и рассылка уходила ВСЕЙ аудитории второй раз. Цена: подписчики
видят дубль, а бот у Telegram выглядит рассыльщиком одинакового текста.

Тот же выбор уже сделан для Growth Agent и для инвайта, и записан в CLAUDE.md:
дедуп необратимого действия читается строго. Блип базы переживаем повторами;
не прочитался совсем — рассылка остаётся в своём статусе и её подберёт докатка
на следующем старте процесса. Отложить дешевле, чем послать дважды.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services import bot_api, broadcaster

TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"
BOT_ID = 556
BC_ID = 78
USERS = [201, 202, 203]


def _source(rel: str) -> str:
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def _run(coro):
    return asyncio.run(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _FakePool:
    """Минимум, который читает `broadcaster.run` до цикла отправки."""

    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def fetchval(self, query, *args):
        if "added_by FROM managed_bots" in query:
            return 1
        return None

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"

    async def executemany(self, query, args):
        return None


class _FakeSession:
    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(broadcaster, "BROADCAST_DELAY", 0, raising=False)
    monkeypatch.setattr(broadcaster, "_GROUP_DELAY", 0, raising=False)

    async def _instant(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _broadcast(delivered):
    """Прогнать рассылку. `delivered` — что отдаёт чтение дедупа (или падает).

    Возвращает (кому реально отправили, какие статусы записаны в рассылку).
    """
    calls: list[int] = []

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT_ID, "username": "b"}}
        calls.append(params.get("chat_id"))
        return {"ok": True, "result": {"message_id": 1}}

    statuses: list[str] = []

    async def _upd(pool, bid, sent, failed, status=None, *a, **kw):
        statuses.append(status)

    reads = {"n": 0}

    async def _delivered(pool, bid):
        reads["n"] += 1
        if isinstance(delivered, Exception):
            raise delivered
        return set(delivered)

    with patch.object(bot_api, "_call", _call):
        with patch("database.db.mark_user_inactive", AsyncMock()):
            with patch("database.db.update_broadcast", _upd):
                with patch("database.db.get_broadcast_delivered_ids", _delivered):
                    with patch("services.brand_injection.is_free_tier",
                               AsyncMock(return_value=False)):
                        _run(broadcaster.run(
                            _FakePool(), _FakeSession(), BC_ID, TOKEN, BOT_ID,
                            "Привет", user_ids=list(USERS),
                        ))
    return calls, statuses, reads["n"]


# ── Главное ──────────────────────────────────────────────────────────────────

def test_an_unreadable_delivery_log_stops_the_broadcast():
    before = _total("infragram_broadcast_dedup_read_failures_total")
    calls, statuses, _reads = _broadcast(RuntimeError("база недоступна"))

    assert calls == [], (
        f"рассылка ушла получателям {calls}, не зная, кому уже отправляли — "
        "на докатке это дубль всей аудитории")
    assert "running" not in statuses, (
        "рассылка отмечена работающей, хотя не отправила никому")
    assert _total("infragram_broadcast_dedup_read_failures_total") > before, (
        "отказ обязан быть виден на графике, а не только в логе")


def test_a_blip_is_survived_by_retrying_not_by_an_empty_list():
    """Короткий сбой базы не повод откладывать рассылку."""
    state = {"left": 2}

    async def _flaky(pool, bid):
        if state["left"] > 0:
            state["left"] -= 1
            raise RuntimeError("блип")
        return {201}

    calls: list[int] = []

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT_ID, "username": "b"}}
        calls.append(params.get("chat_id"))
        return {"ok": True, "result": {"message_id": 1}}

    with patch.object(bot_api, "_call", _call):
        with patch("database.db.mark_user_inactive", AsyncMock()):
            with patch("database.db.update_broadcast", AsyncMock()):
                with patch("database.db.get_broadcast_delivered_ids", _flaky):
                    with patch("services.brand_injection.is_free_tier",
                               AsyncMock(return_value=False)):
                        _run(broadcaster.run(
                            _FakePool(), _FakeSession(), BC_ID, TOKEN, BOT_ID,
                            "Привет", user_ids=list(USERS),
                        ))

    assert state["left"] == 0, "повторов чтения не было — блип откладывал рассылку"
    assert 201 not in calls, "уже доставленному отправили второй раз"
    assert calls == [202, 203], calls


def test_a_readable_log_lets_the_broadcast_through():
    """Проверка выше была бы вечно зелёной, если рассылка не идёт никогда."""
    calls, statuses, reads = _broadcast([])
    assert calls == USERS, calls
    assert reads == 1, "здоровое чтение не должно повторяться"


# ── Журнал доставок не должен отставать от отправок ──────────────────────────

def test_every_delivery_is_journalled_before_the_next_one_goes_out():
    """Пачка по 25 держала записи в памяти процесса десятки секунд.

    Рассылка идёт с паузами против лимитов Telegram, SIGTERM на деплое
    приходит в это окно штатно — и докатка писала этим людям второй раз,
    потому что в журнале их не было.
    """
    journalled: list[int] = []
    # Снимок журнала ПЕРЕД каждой отправкой. Сравнивать итог бессмысленно: по
    # возврату рассылка сбрасывает буфер сама, и при любой пачке итог сойдётся
    # — ровно та ложная зелень, которой проверка «итог равен списку» и была.
    before_each: list[tuple[int, tuple]] = []

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT_ID, "username": "b"}}
        before_each.append((params.get("chat_id"), tuple(journalled)))
        return {"ok": True, "result": {"message_id": 1}}

    class _Pool(_FakePool):
        async def executemany(self, query, args):
            if "broadcast_delivery_log" in query:
                journalled.extend(a[1] for a in args)
            return None

    with patch.object(bot_api, "_call", _call):
        with patch("database.db.mark_user_inactive", AsyncMock()):
            with patch("database.db.update_broadcast", AsyncMock()):
                with patch("database.db.get_broadcast_delivered_ids",
                           AsyncMock(return_value=set())):
                    with patch("services.brand_injection.is_free_tier",
                               AsyncMock(return_value=False)):
                        _run(broadcaster.run(
                            _Pool(), _FakeSession(), BC_ID, TOKEN, BOT_ID,
                            "Привет", user_ids=list(USERS),
                        ))

    assert [u for u, _ in before_each] == USERS, before_each
    for i, (uid, snapshot) in enumerate(before_each):
        assert list(snapshot) == USERS[:i], (
            f"перед отправкой {uid} в журнале {list(snapshot)}, а должно быть "
            f"{USERS[:i]}: доставки копятся в памяти процесса, и обрыв в этом "
            "окне стоит их дублей на докатке")
    assert journalled == USERS, journalled


def test_the_journal_is_not_batched_behind_the_sends():
    """Храповик: возврат пачки вернёт окно дублей молча."""
    assert broadcaster is not None
    src = _source("services/broadcaster.py")
    import re
    m = re.search(r"_DELIVERY_FLUSH_INTERVAL = (\d+)", src)
    assert m, "интервал сброса журнала доставок пропал"
    assert int(m.group(1)) == 1, (
        f"журнал сбрасывается раз в {m.group(1)} отправок — столько доставок "
        "живёт только в памяти процесса, и обрыв стоит их дублей")


def test_a_lost_journal_row_is_counted_not_swallowed():
    """Потерянная строка журнала = дубль на докатке; знать об этом надо сразу."""
    before = _total("infragram_journal_write_failures_total")

    async def _call(session, token, method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": BOT_ID, "username": "b"}}
        return {"ok": True, "result": {"message_id": 1}}

    class _Pool(_FakePool):
        async def executemany(self, query, args):
            if "broadcast_delivery_log" in query:
                raise RuntimeError("база отклонила запись")
            return None

    sent_to: list[int] = []

    async def _counting(session, token, method, **params):
        if method != "getMe":
            sent_to.append(params.get("chat_id"))
        return await _call(session, token, method, **params)

    with patch.object(bot_api, "_call", _counting):
        with patch("database.db.mark_user_inactive", AsyncMock()):
            with patch("database.db.update_broadcast", AsyncMock()):
                with patch("database.db.get_broadcast_delivered_ids",
                           AsyncMock(return_value=set())):
                    with patch("services.brand_injection.is_free_tier",
                               AsyncMock(return_value=False)):
                        _run(broadcaster.run(
                            _Pool(), _FakeSession(), BC_ID, TOKEN, BOT_ID,
                            "Привет", user_ids=list(USERS),
                        ))

    assert _total("infragram_journal_write_failures_total") > before, (
        "сбой журнала виден только в логе — о дублях узнаем из жалобы")
    assert sent_to == USERS, (
        "сбой журнала оборвал рассылку: отправленного не отменить, и терять "
        "из-за записи в базу остаток аудитории незачем")
