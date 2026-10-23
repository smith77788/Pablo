"""Гейт карантина не отвечает «все чисты» с первого же блипа, и не молча.

Перед каждой массовой операцией продукт спрашивает, кто из флота под
карантином: аккаунт с подтверждённым ограничением (ban/block/restrict) брать в
работу нельзя — это прямой путь к потере аккаунта. Проверка fail-open: нет
данных — никого не держим, иначе anti-detection ядро встало бы от отсутствия
пульса. Выбор осознанный, но у него было два изъяна:

  • решал ОДИН запрос. Блип к базе читался как «все чисты», и операция уходила
    работать ограниченными аккаунтами;
  • запись об этом шла в `debug`. Предохранитель, отключившийся молча, снаружи
    не отличить от предохранителя, который разрешил.

Проверяется и то и другое: один сбой лечится повтором, два сбоя оставляют
fail-open, но громко.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """Пул, который падает на первых `fail` чтениях."""

    def __init__(self, fail: int, rows: list[dict] | None = None):
        self.fail = fail
        self.rows = rows or []
        self.reads = 0

    async def fetch(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась")
        return self.rows


def test_a_single_blip_is_cured_by_the_retry():
    """Один сбой — не причина объявить ограниченный аккаунт чистым."""
    from services import infra_memory

    pool = _Pool(fail=1, rows=[{"account_id": 7}])
    out = _run(infra_memory.quarantined_accounts(pool, [7, 8]))

    assert out == {7}, (
        f"гейт ответил «под карантином: {out or 'никого'}» после одного блипа — "
        "аккаунт 7 с подтверждённым ограничением уйдёт в работу")
    assert pool.reads == 2, f"повтора не было: чтений {pool.reads}"


def test_an_unreadable_gate_says_so_out_loud(caplog):
    """Остались при fail-open — но это видно, а не спрятано в debug."""
    from services import infra_memory

    pool = _Pool(fail=99)
    with caplog.at_level(logging.ERROR, logger="services.infra_memory"):
        out = _run(infra_memory.quarantined_accounts(pool, [7, 8]))

    assert out == set(), "fail-open сохраняется: флот не держим из-за сбоя чтения"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "отключившийся предохранитель не оставил записи уровня error — снаружи "
        "его не отличить от предохранителя, который разрешил работу")


def test_the_single_account_gate_rides_the_same_check():
    """Поштучная проверка — частный случай пакетной, с тем же повтором."""
    from services import infra_memory

    pool = _Pool(fail=1, rows=[{"account_id": 7}])
    assert _run(infra_memory.is_account_quarantined(pool, 7)) is True
    assert pool.reads == 2, f"повтора не было: чтений {pool.reads}"


@pytest.mark.parametrize("bad", [None, 0])
def test_an_empty_request_costs_no_query(bad):
    """Пустой вход — не повод ходить в базу (и не повод считать это сбоем)."""
    from services import infra_memory

    pool = _Pool(fail=99)
    assert _run(infra_memory.quarantined_accounts(pool, [bad] if bad else [])) == set()
    assert pool.reads == 0
