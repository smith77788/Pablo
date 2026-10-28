"""История удара — не только отчёт, и её потеря не молчит.

`strike_history` читает `strike_engine.is_strike_allowed`: гейт смотрит, не
бита ли цель за последние часы, и не пускает повторный удар — это и
детекшн-паттерн, и лишний износ живых аккаунтов. Потерянная строка выключает
интервал для конкретной цели: следующий заход по ней пройдёт как первый.

Прежде сбой записи уходил в warning с формулировкой «не видно в History tab»,
то есть последствие выглядело косметическим, а повтора записи не было вовсе.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace


def _run(coro):
    return asyncio.run(coro)


def _result(target: str = "@target"):
    """Итог удара в том виде, в каком его отдаёт движок."""
    return SimpleNamespace(
        target=target, unique_accounts=3, peer_reported=3, msgs_reported=12,
        msgs_fetched=40, pinned_reported=1, admins_reported=2, network_nodes=0,
        network_reports=0, blocked=0, verified_down=None, duration_s=120,
        abuse_form_ok=True, spambot_escalation=0, accounts_ok=3,
        accounts_flood=0, accounts_banned=0, accounts_failed=0,
        infra_domains=[], infra_apwg_sent=0, infra_registrar_sent=0,
    )


class _Pool:
    def __init__(self, fail: int):
        self.fail = fail
        self.writes = 0

    async def execute(self, query, *args):
        self.writes += 1
        if self.writes <= self.fail:
            raise ConnectionResetError("БД отвалилась")
        return "INSERT 0 1"


def test_a_single_blip_does_not_lose_the_history():
    """Один сбой — не причина забыть, что по цели уже били."""
    from services import op_worker

    pool = _Pool(fail=1)
    ok = _run(op_worker._write_strike_history(pool, 42, 77, "spam", None, _result()))

    assert ok is True, (
        "история удара потеряна после одного блипа: интервал повтора цель не "
        "увидит, и следующий заход пройдёт как первый")
    assert pool.writes == 2, f"повтора не было: записей {pool.writes}"


def test_a_lost_history_says_so_out_loud(caplog, monkeypatch):
    """Не записали и со второго раза — error и счётчик, а не косметика."""
    from services import op_worker

    seen: list[str] = []
    monkeypatch.setattr(
        op_worker, "_reliability_metric",
        lambda name, **labels: seen.append(name))

    with caplog.at_level(logging.ERROR, logger="services.op_worker"):
        ok = _run(op_worker._write_strike_history(
            _Pool(fail=99), 42, 77, "spam", None, _result()))

    assert ok is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "потеря истории не оставила записи уровня error")
    assert "infragram_strike_history_write_failures_total" in seen, (
        f"счётчик потери не вырос: {seen} — владелец об этом не узнает")


def test_the_healthy_path_writes_once():
    """Здоровый путь: одна запись."""
    from services import op_worker

    pool = _Pool(fail=0)
    assert _run(op_worker._write_strike_history(
        pool, 42, 77, "spam", None, _result())) is True
    assert pool.writes == 1


def test_the_write_never_breaks_the_operation():
    """Удар уже сделан — операцию из-за записи не теряем."""
    from services import op_worker

    class _Dead:
        async def execute(self, query, *args):
            raise RuntimeError("пул закрыт")

    assert _run(op_worker._write_strike_history(
        _Dead(), 42, 77, "spam", None, _result())) is False
