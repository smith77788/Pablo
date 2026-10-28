"""Заблокировавший бота не получает рассылок, а вернувшийся получает снова.

ЧТО БЫЛО. Продукт узнал о блокировке (`my_chat_member` → `db.block_user`), но
ставил ровно один флаг — `bot_users.is_blocked`. А отбор адресатов почти везде
смотрит на другой флаг, `is_active`:

  * `op_worker._exec_run_broadcast` — обычная рассылка;
  * `op_worker` network_broadcast — рассылка по всей сети ботов;
  * `broadcaster.resend_undelivered` — повтор недоставленным;
  * `dm_engine._get_targets` — личные сообщения по подписчикам бота;
  * `db.get_audience_new_users` / `get_audience_by_language` — сегменты в боте.

То есть заблокировавший оставался в аудитории и получал ещё одну попытку
доставки — ответ 403 на каждой рассылке. Цена не в лишнем запросе: для
Telegram повторный стук туда, откуда бота выгнали, — ровно тот рисунок, по
которому рассылку считают спамом.

Обратная сторона была хуже. `mark_user_inactive` (её зовёт цикл рассылки на
недоставку из `RECIPIENT_GONE`, включая «не начинал диалог») ставила
`is_active=FALSE`, а поднять флаг обратно было НЕКОМУ: `upsert_users`, которая
отрабатывает на каждое входящее сообщение, колонку не трогала. Человек, once
помеченный, писал боту, переписывался с ним — и всё равно навсегда выпадал из
всех рассылок. Молча.

ЧТО СТАЛО. Флаги согласованы: блокировка гасит оба, возврат и живое сообщение
боту поднимают оба, недоставка «заблокировал» оставляет след в `is_blocked`, а
отбор адресатов и счётчики «сколько получат» фильтруют заблокированных.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from database import db


def _run(coro):
    return asyncio.run(coro)


class _RecordingPool:
    """Пул, который ничего не делает, но помнит каждый запрос и аргументы."""

    def __init__(self, fetch_rows=None, fetchval=None):
        self.queries: list[tuple[str, tuple]] = []
        self._fetch_rows = fetch_rows if fetch_rows is not None else []
        self._fetchval = fetchval

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "UPDATE 1"

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return list(self._fetch_rows)

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return None

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return self._fetchval

    async def executemany(self, query, args):
        self.queries.append((query, tuple(args)))
        return None

    def acquire(self):
        return _Acquire(self)

    def sql(self) -> str:
        return "\n".join(q for q, _ in self.queries)


class _Acquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, *exc):
        return False


# ── флаги согласованы между собой ────────────────────────────────────────────

def test_block_also_makes_the_subscriber_unreachable():
    """Блокировка — это не только пометка, но и «писать больше некуда»."""
    pool = _RecordingPool()
    _run(db.block_user(pool, 10, 777, True))
    sql, args = pool.queries[0]
    assert "is_blocked" in sql and "is_active" in sql, (
        "block_user трогает только is_blocked — отбор адресатов смотрит на "
        f"is_active и заблокировавший остаётся в аудитории: {sql}"
    )
    assert args[-1] is True


def test_return_from_block_brings_the_subscriber_back():
    """Вышел из блокировки — снова адресат, иначе он потерян навсегда."""
    pool = _RecordingPool()
    _run(db.block_user(pool, 10, 777, False))
    sql, args = pool.queries[0]
    assert "is_active" in sql, sql
    assert args[-1] is False


def test_block_failure_leaves_a_trace_in_is_blocked():
    """403 «bot was blocked» — это блокировка, а не «аккаунт удалён»."""
    pool = _RecordingPool()
    _run(db.mark_user_inactive(pool, 10, 777, reason="blocked"))
    sql = " ".join(pool.queries[0][0].split())
    assert "is_blocked=TRUE" in sql, (
        "след блокировки теряется — она неотличима от удалённого аккаунта: "
        + sql)
    assert "is_active=FALSE" in sql


def test_other_delivery_failures_do_not_claim_a_block():
    """«Аккаунт удалён» и «не начинал диалог» блокировкой не считаются."""
    for reason in ("deactivated", "not_started", ""):
        pool = _RecordingPool()
        _run(db.mark_user_inactive(pool, 10, 777, reason=reason))
        sql, _ = pool.queries[0]
        assert "is_blocked" not in sql, (
            f"причина {reason!r} выдаётся за блокировку: {sql}")


def test_mark_user_inactive_reason_is_optional():
    """Старые вызовы без причины должны работать по-прежнему."""
    pool = _RecordingPool()
    _run(db.mark_user_inactive(pool, 10, 777))
    assert "is_active=FALSE" in pool.queries[0][0]


def test_writing_to_the_bot_restores_reachability():
    """Пометка была необратимой: поднять is_active обратно было некому."""
    pool = _RecordingPool()
    _run(db.batch_upsert_users(pool, 10, [{"user_id": 777}]))
    sql = pool.sql()
    assert "ON CONFLICT" in sql
    flat = " ".join(sql.split())
    assert "is_active = NOT bot_users.is_blocked" in flat, (
        "upsert подписчика не возвращает его в аудиторию — человек, однажды "
        f"помеченный неактивным, выпадает из рассылок навсегда: {sql}")


def test_old_updates_do_not_resurrect_a_blocked_subscriber():
    """Сбор аудитории перечитывает старый хвост getUpdates.

    Сообщение годичной давности не должно возвращать в рассылки того, кто
    успел заблокировать бота: снять блокировку может только сам Telegram
    обновлением my_chat_member.
    """
    pool = _RecordingPool()
    _run(db.batch_upsert_users(pool, 10, [{"user_id": 777}]))
    flat = " ".join(pool.sql().split())
    assert "is_blocked = FALSE" not in flat and "is_blocked=FALSE" not in flat, (
        "upsert снимает блокировку — старый апдейт воскресит ушедшего: " + flat)


# ── отбор адресатов читает эту отметку ───────────────────────────────────────

def test_bot_segments_skip_blocked():
    """Сегменты «новые» и «по языку» в боте — это списки адресатов."""
    for call in (
        lambda p: db.get_audience_new_users(p, 10, 7),
        lambda p: db.get_audience_by_language(p, 10, "ru"),
    ):
        pool = _RecordingPool()
        _run(call(pool))
        assert "is_blocked" in pool.sql(), pool.sql()


def test_resend_undelivered_skips_blocked():
    from services import broadcaster

    src = inspect.getsource(broadcaster.resend_undelivered)
    assert "bot_users" in src
    _sel = src[src.index("FROM bot_users"):]
    assert "is_blocked" in _sel, (
        "повтор недоставленным снова постучится к заблокировавшим:\n" + _sel[:400])


def test_broadcast_executor_skips_blocked():
    from services import op_worker

    src = inspect.getsource(op_worker._exec_run_broadcast)
    _sel = src[src.index("FROM bot_users"):]
    assert "is_blocked" in _sel, (
        "исполнитель рассылки берёт заблокировавших:\n" + _sel[:400])


def test_direct_messages_skip_blocked():
    """Заблокировал бота — дослать то же с личного аккаунта нельзя."""
    from services import dm_engine

    src = inspect.getsource(dm_engine._get_targets)
    for marker in ("bu.bot_id=$1 AND mb.added_by=$2", "mb.added_by=$1"):
        assert marker in src, marker
        tail = src[src.index(marker): src.index(marker) + 400]
        assert "is_blocked" in tail, (
            f"источник {marker!r} тянет заблокировавших:\n{tail}")


def test_network_broadcast_skips_blocked():
    """Рассылка по всей сети ботов — два отбора: все и по языку."""
    import pathlib

    from services import op_worker

    src = pathlib.Path(op_worker.__file__).read_text()
    picks = [
        line for line in src.splitlines()
        if "SELECT user_id FROM bot_users" in line
    ]
    assert picks, "отбор адресатов из bot_users исчез — проверьте тест"
    for line in picks:
        idx = src.index(line)
        assert "is_blocked" in src[idx: idx + len(line) + 200], (
            "отбор адресатов не фильтрует заблокировавших:\n" + line)


# ── счётчик «сколько получат» не обещает больше, чем уйдёт ───────────────────

@pytest.mark.parametrize("fn_name", ["mass_broadcast_with_scheduling"])
def test_promised_total_matches_the_real_audience(fn_name):
    from services import broadcaster

    src = inspect.getsource(getattr(broadcaster, fn_name))
    _cnt = src[src.index("COUNT(*) FROM bot_users"):]
    assert "is_blocked" in _cnt[:300], (
        "обещанное число получателей считает заблокировавших:\n" + _cnt[:300])
