"""Операция, которую откладывают сутками, перестаёт быть невидимой.

РАЗРЫВ. Сторож застрявших операций намеренно НЕ считает застрявшей операцию,
которая ждёт своего `scheduled_for`: отложенные операции — штатное состояние
(инвайт «продолжим завтра», повтор после cooldown), и без этого условия каждая
продолжающаяся операция сыпала ложным алертом.

Но тем же полем `scheduled_for` пользуются все пути ОТКЛАДЫВАНИЯ: флуд-пауза
(`_defer_op_for_flood` — PeerFlood откладывает на 48 часов), повтор после
ошибки, пауза предохранителя. Каждый из них переносит срок ВПЕРЁД. Значит
операция, которую откладывают снова и снова, по построению не попадает под
критерий «застряла» никогда: сколько бы суток она ни висела, её `scheduled_for`
всегда в будущем.

Владельцу об этом говорят ровно один раз — `_defer_op_for_flood` присылает
«отложена, продолжится сама», с окном анти-повтора по тому же op_id. Дальше
тишина. Если флот ограничен Telegram целиком, операция так и ходит кругами:
ни одной цели, ни одного слова, ни одного алерта. Для продукта, где массовые
операции и есть продукт, это потерянная работа, о которой никто не узнает.

ЧТО ОТЛИЧАЕТ ЭТОТ СЛУЧАЙ ОТ ЗАКОННОГО ОЖИДАНИЯ. Срок, выбранный ВЛАДЕЛЬЦЕМ
(«запусти в пятницу»), ставится при постановке и не сопровождается причиной:
`last_error` у такой операции пуст. Срок, который перенесли МЫ, всегда
приходит вместе с причиной. Поэтому отложенная нами операция старше порога
объявляется, а запланированная владельцем на месяц вперёд — нет.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяется семантика запроса-детектора: какие именно
строки под него попадают и, главное, какие НЕ попадают (детектор, дающий
находки в зрелом коде, почти всегда сломан). Заглушка пула SQL не исполняет.
Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991780
ADMIN = 991781
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


class _Bot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(str(text))


@pytest.fixture
def alerts(pool, monkeypatch):
    """Сторож алертов с подменёнными админами и чистым окном анти-повтора."""
    from services import op_worker
    from bot.utils import subscription as _sub

    monkeypatch.setattr(_sub, "_admin_ids", lambda: {ADMIN})
    bot = _Bot()

    def _go():
        _run(op_worker._watchdog_alerts(pool, bot))
        return "\n".join(bot.sent)

    return _go


@pytest.fixture(autouse=True)
def _clean(pool):
    def _wipe():
        _run(pool.execute(
            "DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
        # Окно анти-повтора: иначе второй тест не получит сообщения вовсе.
        for t in ("notify_dedup", "notification_dedup"):
            try:
                _run(pool.execute(f"DELETE FROM {t} WHERE user_id=$1", ADMIN))
            except Exception:
                pass
    _wipe()
    yield
    _wipe()


async def _op(pool, *, status: str, age_h: float, sched_h: float | None,
              last_error: str | None) -> int:
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, "
        "total_items, done_items) "
        "VALUES($1,'mass_invite','{}'::jsonb,$2,100,0) RETURNING id",
        OWNER, status)
    await pool.execute(
        "UPDATE operation_queue "
        "   SET created_at = now() - make_interval(hours => $2::int), "
        "       started_at = CASE WHEN $4 = 'running' "
        "                         THEN now() - make_interval(hours => $2::int) END, "
        "       scheduled_for = CASE WHEN $3::float IS NULL THEN NULL "
        "                            ELSE now() + make_interval(hours => $3::int) END, "
        "       last_error = $5 "
        " WHERE id = $1",
        op_id, int(age_h), (None if sched_h is None else float(sched_h)),
        status, last_error)
    return op_id


# ── Главное: откладываемая сутками операция объявляется ──────────────────────

def test_an_operation_we_keep_postponing_for_days_is_reported(pool, alerts):
    """Иначе о потерянной работе не узнаёт никто и никогда."""
    op_id = _run(_op(pool, status="pending", age_h=100, sched_h=10,
                     last_error="FloodWait: подождите 48 ч"))
    text = alerts()
    assert f"#{op_id}" in text, (
        "операцию откладывают пятые сутки, и сторож её не видит — "
        f"сообщение админу: {text!r}")


def test_the_reason_we_postponed_it_reaches_the_human(pool, alerts):
    """«Застряла» без причины — это повод отменить и запустить заново (хуже)."""
    op_id = _run(_op(pool, status="pending", age_h=100, sched_h=10,
                     last_error="FloodWait: подождите 48 ч"))
    text = alerts()
    assert f"#{op_id}" in text and "FloodWait" in text, (
        f"причина откладывания не доехала: {text!r}")


# ── Чего детектор делать не должен ───────────────────────────────────────────

def test_an_operation_the_owner_scheduled_for_later_is_left_alone(pool, alerts):
    """Срок владельца приходит без причины — её операция не застряла."""
    op_id = _run(_op(pool, status="pending", age_h=100, sched_h=240,
                     last_error=None))
    text = alerts()
    assert f"#{op_id}" not in text, (
        "запланированная владельцем операция объявлена застрявшей")


def test_a_normal_short_postponement_is_left_alone(pool, alerts):
    """Штатная флуд-пауза — не авария: ровно от этого ложняка и уходили."""
    op_id = _run(_op(pool, status="pending", age_h=2, sched_h=1,
                     last_error="FloodWait: подождите 60 мин"))
    text = alerts()
    assert f"#{op_id}" not in text, (
        "обычная пауза Telegram объявлена застрявшей — вернулся ложный алерт")


def test_a_finished_operation_is_left_alone(pool, alerts):
    """Терминальная операция никуда не идёт и никого не ждёт."""
    op_id = _run(_op(pool, status="done", age_h=100, sched_h=10,
                     last_error="FloodWait"))
    text = alerts()
    assert f"#{op_id}" not in text, "объявлена застрявшей завершённая операция"


# ── Самопроверка: прежние два класса ещё видны ───────────────────────────────

def test_the_old_stuck_pending_is_still_reported(pool, alerts):
    """Проверка измерителя: новое условие не должно съесть прежние."""
    op_id = _run(_op(pool, status="pending", age_h=3, sched_h=None,
                     last_error=None))
    text = alerts()
    assert f"#{op_id}" in text, (
        "обычная застрявшая ожидающая операция больше не объявляется")


def test_the_probe_sees_nothing_in_a_healthy_queue(pool, alerts):
    """Если бы детектор объявлял всё подряд, тесты выше ничего не измеряли."""
    op_id = _run(_op(pool, status="pending", age_h=0, sched_h=None,
                     last_error=None))
    text = alerts()
    assert f"#{op_id}" not in text, (
        "свежая операция объявлена застрявшей — детектор сломан")
