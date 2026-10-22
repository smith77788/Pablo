"""Строка мини-удара в истории не теряется молча.

РАЗРЫВ. `strike_history` — это не только вкладка «История» в UI. ЭТУ ЖЕ
строку читает `is_strike_allowed` (по колонке `target`), решая, не бьём ли мы
цель повторно внутри интервала. У большого удара запись уже была с повтором и
громким отказом, а мини-удар (`execute_mini_strike`) писал свою строку
обычным `pool.execute` в `try/except → log.debug`. Потерянная строка означала,
что следующий заход по той же цели пройдёт как ПЕРВЫЙ: повторные жалобы с
живых аккаунтов внутри интервала — и детекшн-паттерн, и лишний износ флота.

ЧТО ПРОВЕРЯЕМ: одна повторная попытка на месте, при полном отказе — `False`,
`log.error` словами о последствии и счётчик, который сторож защитных записей
превращает в сообщение владельцу. И что цель с причиной реально попадают в
строку: гейт ищет именно по `target`.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

_TG = {"peer_reported": True, "msg_reported": 7, "msgs_fetched": 20,
       "pinned_reported": 1, "admins_reported": 2, "blocked": True}
_RES = {"abuse_form": {"ok": True}}


class _Pool:
    def __init__(self, fail: int = 0):
        self.fail = int(fail)
        self.calls = 0
        self.args: tuple = ()

    async def execute(self, query, *args):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("соединение с БД потеряно")
        self.args = (query,) + args
        return "INSERT 0 1"


@pytest.fixture
def se(monkeypatch):
    from services import metrics, strike_engine

    metrics.reset()
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(strike_engine.asyncio, "sleep",
                        lambda *_a, **_k: _real_sleep(0))
    return strike_engine


def _write(se, pool):
    return asyncio.run(se._write_mini_strike_history(
        pool, 42, "@scam_channel", "fraud", _TG, _RES))


def test_a_blip_is_retried_and_the_row_lands(se):
    pool = _Pool(fail=1)
    assert _write(se, pool) is True, "мгновенный сбой похоронил строку истории"
    assert pool.calls == 2, f"повторной попытки не было (попыток {pool.calls})"


def test_the_row_carries_the_target_the_gate_reads_by(se):
    pool = _Pool()
    _write(se, pool)
    assert "INSERT INTO strike_history" in pool.args[0]
    assert "@scam_channel" in pool.args, (
        "гейт интервала ищет по target — без него строка ему не видна")
    assert 42 in pool.args and "fraud" in pool.args


def test_a_lost_row_is_reported_and_not_claimed_as_written(se, caplog):
    from services import metrics

    pool = _Pool(fail=2)
    with caplog.at_level(logging.ERROR):
        ok = _write(se, pool)

    assert ok is False, "потерянная строка выдана вызывающему за записанную"
    assert pool.calls == 2, "попыток должно быть ровно две"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "история удара НЕ ЗАПИСАНА" in text, text
    assert "как первый" in text, "в логе не названо последствие"

    counters = metrics.snapshot()["counters"]
    hit = [k for k in counters
           if k.startswith("infragram_strike_history_write_failures_total")]
    assert hit, "счётчик потерянной истории не вырос — владельцу не скажут"


def test_the_mini_strike_goes_through_the_writer(se):
    """Иначе исправленной записи просто никто не позовёт."""
    import inspect

    src = inspect.getsource(se.execute_mini_strike)
    assert "_write_mini_strike_history(" in src
    assert "INSERT INTO strike_history" not in src, (
        "мини-удар снова пишет историю сам, в обход писателя с повтором")
