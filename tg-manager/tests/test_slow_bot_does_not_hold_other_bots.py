"""Медленный бот не держит опрос остальных ботов.

Проход опроса ждал ВСЕХ ботов разом (`asyncio.gather`), то есть шёл со
скоростью самого медленного: ИИ-продавец, думающий 30 секунд, у одного
клиента задерживал авто-ответы всех ботов всех клиентов, а быстрый опрос
«горячих» ботов (счёт сценария: на pre_checkout Telegram даёт 10 секунд)
стоял за этим проходом. Теперь каждый бот — своя задача, а занятый бот
пропускается, а не опрашивается второй раз параллельно.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from services import auto_responder as ar


async def test_launch_returns_at_once_and_fast_bot_is_not_held():
    done: list[int] = []
    slow_gate = asyncio.Event()

    async def _fake_process(pool, http, bot_id, token, main_bot=None, updates=None):
        if bot_id == 1:
            await slow_gate.wait()          # «ИИ думает»
        done.append(bot_id)

    bots = [{"bot_id": 1, "token": "t1"}, {"bot_id": 2, "token": "t2"}]
    with patch.object(ar, "_process_bot", _fake_process):
        n = ar._launch_polls(None, None, bots)
        assert n == 2
        await asyncio.sleep(0.05)
        assert done == [2], "быстрый бот обработан, пока медленный занят"

        # Следующий проход: медленный всё ещё занят — его не трогаем
        # (двойной getUpdates обработал бы пакет дважды), быстрый — снова.
        n2 = ar._launch_polls(None, None, bots)
        assert n2 == 1
        await asyncio.sleep(0.05)
        assert done == [2, 2]

        slow_gate.set()
        await asyncio.wait_for(asyncio.gather(*list(ar._poll_tasks)), 2)
    assert sorted(done) == [1, 2, 2]
    assert not ar._poll_tasks and not ar._bot_busy


async def test_crashed_poll_is_logged_and_released(caplog):
    async def _boom(*a, **k):
        raise RuntimeError("сбой опроса")

    with patch.object(ar, "_process_bot", _boom):
        ar._launch_polls(None, None, [{"bot_id": 9, "token": "t"}])
        await asyncio.sleep(0.05)
    assert 9 not in ar._bot_busy, "упавший опрос освобождает бота"
    assert not ar._poll_tasks
    assert any("опрос бота упал" in r.getMessage() for r in caplog.records)


def test_run_loop_does_not_wait_for_all_bots():
    import inspect

    src = inspect.getsource(ar.run)
    assert "_launch_polls(" in src
    assert "_poll_one(" not in src, "проход снова ждёт каждого бота"
