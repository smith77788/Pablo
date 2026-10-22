"""Интервал повтора удара не отменяется одним блипом к базе, и не молча.

Перед каждым ударом (кроме сознательного `force`) исполнитель спрашивает, не
била ли операция эту цель в последние несколько часов: повторный удар — и
детекшн-паттерн, и лишний износ живых аккаунтов. Гейт fail-open по замыслу:
удар не необратимая рассылка, и отказ из-за отсутствия данных стоил бы
владельцу операции. Но решал гейт ОДИН запрос, а запись о сбое шла в warning
без указания, что именно случилось.

Проверяется и то и другое: один сбой лечится повтором, два сбоя оставляют
fail-open, но громко и с объяснением.
"""
from __future__ import annotations

import asyncio
import logging


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """Пул, который падает на первых `fail` чтениях, а дальше отдаёт строку."""

    def __init__(self, fail: int, row=None):
        self.fail = fail
        self.row = row
        self.reads = 0

    async def fetchrow(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась")
        return self.row


def test_a_single_blip_does_not_open_the_interval():
    """Один сбой — не причина разрешить повторный удар."""
    from services import strike_engine

    pool = _Pool(fail=1, row={"?column?": 1})  # удар по цели уже был
    allowed = _run(strike_engine.is_strike_allowed(pool, "@target", 4))

    assert allowed is False, (
        "гейт разрешил удар после одного блипа, хотя цель бита в последние 4ч: "
        "это детекшн-паттерн и износ живых аккаунтов")
    assert pool.reads == 2, f"повтора не было: чтений {pool.reads}"


def test_an_unreadable_gate_says_so_out_loud(caplog):
    """Остались при fail-open — но это видно, а не спрятано в warning без слов."""
    from services import strike_engine

    pool = _Pool(fail=99)
    with caplog.at_level(logging.ERROR, logger="services.strike_engine"):
        allowed = _run(strike_engine.is_strike_allowed(pool, "@target", 4))

    assert allowed is True, "fail-open сохраняется: операцию из-за сбоя не теряем"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "отключившийся гейт не оставил записи уровня error — снаружи его не "
        "отличить от гейта, который разрешил удар по существу")


def test_a_clean_target_is_allowed():
    """Здоровый путь: цель не бита — удар разрешён одним чтением."""
    from services import strike_engine

    pool = _Pool(fail=0, row=None)
    assert _run(strike_engine.is_strike_allowed(pool, "@target", 4)) is True
    assert pool.reads == 1
