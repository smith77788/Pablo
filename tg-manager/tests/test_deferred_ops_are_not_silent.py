"""Отложенная и не запустившаяся операция объясняется владельцу, а не молчит.

ЧТО БЫЛО. Три исхода операции делают в `_run_op_task` ранний `return` и потому
НЕ доходят до общего блока финальных уведомлений — он ниже по коду. Целый класс
судеб уходил в тишину:

  1. Флот занят дольше предела → операция помечалась `failed` и умирала
     совершенно молча. Владелец видел «ожидает», потом ничего.
  2. Длинная флуд-пауза (PeerFlood откладывает на 48 часов) → операция
     показывалась как «ожидает» без единого слова о том, почему и до каких пор.
  3. Открытый предохранитель → ВСЕ операции владельца на полчаса уходили в
     «ожидает» и не стартовали. Снаружи это ровно «ничего не работает».

Во втором и третьем случае молчание обходится дороже сообщения: не понимая, что
происходит, владелец отменяет, перезапускает и заводит новые операции — то есть
делает именно то, от чего пауза и предохранитель его уберегают, и добирает новых
ограничений Telegram.
"""
from __future__ import annotations

import datetime as dt

import pytest


class _Bot:
    pass


class _Pool:
    def __init__(self, waiting_since=None):
        self.row = {"acct_wait_since": waiting_since}

    async def fetchrow(self, query, *args):
        return self.row

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.fixture
def sent(monkeypatch):
    from services import op_worker

    out: list[tuple[int, str]] = []

    async def _notify(pool, bot, owner_id, kind, text, **kw):
        out.append((owner_id, text))

    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _notify)
    return out


@pytest.mark.asyncio
async def test_fleet_timeout_failure_reaches_the_owner(sent):
    from services import op_worker

    pool = _Pool(
        waiting_since=dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(minutes=op_worker._ACCT_WAIT_MAX_MIN + 5)
    )
    await op_worker._requeue_op_no_accounts(pool, 7, bot=_Bot(), owner_id=555)

    assert sent, "операция владельца умерла молча — он видел «ожидает», потом ничего"
    owner, text = sent[0]
    assert owner == 555
    assert "#7" in text and "занят" in text


@pytest.mark.asyncio
async def test_short_requeue_stays_quiet(sent):
    """Короткое ожидание флота владельца не касается — операция всё равно стартует."""
    from services import op_worker

    await op_worker._requeue_op_no_accounts(_Pool(), 7, bot=_Bot(), owner_id=555)
    assert not sent, "техническая пауза в 90 секунд не повод писать владельцу"


@pytest.mark.asyncio
async def test_long_flood_pause_is_explained(sent):
    from services import op_worker

    await op_worker._defer_op_for_flood(
        _Pool(), 7, 48 * 3600, "PeerFlood", bot=_Bot(), owner_id=555)

    assert sent, "пауза на 48 часов показана как «ожидает» без объяснения"
    _, text = sent[0]
    assert "48 ч" in text
    assert "заново" in text, (
        "владельцу не сказано главное: перезапускать не нужно, повтор только "
        "добавит ограничений"
    )


@pytest.mark.asyncio
async def test_short_flood_pause_stays_quiet(sent):
    from services import op_worker

    await op_worker._defer_op_for_flood(
        _Pool(), 7, 120, "FloodWait", bot=_Bot(), owner_id=555)
    assert not sent


@pytest.mark.asyncio
async def test_circuit_pause_is_explained_once_per_owner(sent):
    from services import op_worker

    op_worker._cb_explained_until.clear()
    try:
        for _ in range(5):
            await op_worker._explain_circuit_pause(_Pool(), _Bot(), 555, 1800)
        assert len(sent) == 1, (
            f"владелец получил {len(sent)} сообщений — цепь останавливает ВСЕ его "
            f"операции, и без троттлинга сообщений будет столько же, сколько операций"
        )
        assert "30 мин" in sent[0][1]
    finally:
        op_worker._cb_explained_until.clear()


@pytest.mark.asyncio
async def test_notification_failure_never_breaks_the_operation(monkeypatch):
    from services import op_worker

    async def _boom(*a, **kw):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _boom)
    await op_worker._notify_owner_about_op(_Pool(), _Bot(), 555, "текст")


@pytest.mark.asyncio
async def test_no_bot_no_crash():
    from services import op_worker

    await op_worker._notify_owner_about_op(_Pool(), None, 555, "текст")
    await op_worker._notify_owner_about_op(_Pool(), _Bot(), None, "текст")
