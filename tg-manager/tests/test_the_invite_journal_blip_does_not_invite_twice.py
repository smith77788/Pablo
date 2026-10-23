"""Регрессия: сбой чтения журнала приглашений больше не зовёт людей второй раз.

РАЗРЫВ. Массовый инвайт перед работой читает `invite_target_log` — кого в эту
группу уже звали. Сбой чтения давал пустой набор, причём беззвучно
(`log_exc_swallow`), с объяснением «лучше не дедупить, чем сорвать инвайт».

Пустой набор означает «в эту группу ещё никого не звали»: операция собирает
аудиторию заново и зовёт тех же людей второй раз. Инвайт — самая баноопасная
операция продукта, повторное приглашение — прямая дорога к PEER_FLOOD и бану
флота. Цена одного сетевого блипа до базы, и следа не оставалось: ни строки
владельцу, ни счётчика.

Теперь — повтор чтения, а затем отказ: операция уходит на обычный повтор
('retry' у классификатора) и позовёт людей позже ровно один раз. Отсрочка
дешевле второго приглашения.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import op_worker
from services.op_errors import _classify_op_error


class _Pool:
    """Пул, который падает на первых `fail` чтениях журнала приглашений."""

    def __init__(self, fail: int, rows: list[dict] | None = None):
        self.fail = fail
        self.rows = rows or []
        self.reads = 0

    async def execute(self, query, *args):
        return "CREATE TABLE"

    async def fetch(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась на чтении журнала")
        return self.rows


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _fast(_s):
        return None
    monkeypatch.setattr(op_worker.asyncio, "sleep", _fast)


@pytest.fixture
def counted(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        op_worker, "_reliability_metric",
        lambda name, n=1, **lbl: seen.append((name, lbl.get("where"))))
    return seen


def test_an_unreadable_invite_journal_postpones_the_run(counted):
    """Не прочитали, кого уже звали — не зовём никого, а откладываем прогон."""
    pool = _Pool(fail=2)
    with pytest.raises(op_worker.JournalUnreadable) as e:
        _run(op_worker._load_invited_targets(pool, 42, "@mygroup"))
    assert pool.reads == 2, f"блип обязан лечиться повтором: чтений {pool.reads}"
    assert _classify_op_error(e.value) == "retry", (
        "отказ обязан вести к повтору операции, а не к её провалу: иначе "
        "владелец получит «не вышло» там, где достаточно было подождать")
    assert ("infragram_invite_dedup_read_failures_total", "mass_invite") in counted, (
        f"самый баноопасный отказ не виден на графике: {counted}")


def test_a_single_blip_is_healed_and_dedup_still_works():
    """Один сбой из двух — журнал прочитан, дедуп действует как обычно."""
    pool = _Pool(fail=1, rows=[{"target": "@Ivan"}])
    assert _run(op_worker._load_invited_targets(pool, 42, "@mygroup")) == {"@ivan"}, (
        "ключи обязаны нормализоваться: '@Ivan' и '@ivan' — один человек")
    assert pool.reads == 2


def test_the_healthy_read_is_unchanged():
    """Журнал читается — набор тот же, что и раньше."""
    pool = _Pool(fail=0, rows=[{"target": "@ivan"}, {"target": "@olga"}])
    assert _run(op_worker._load_invited_targets(pool, 42, "@mygroup")) == {"@ivan", "@olga"}
    assert pool.reads == 1


def test_the_owners_own_choice_to_skip_dedup_is_untouched():
    """Снял дедуп сам — функция не зовётся, и отказа быть не может.

    Это важно: fail-closed не имеет права превратить осознанный выбор владельца
    («звать всех заново») в отложенную операцию.
    """
    src = inspect.getsource(op_worker)
    i = src.index("_skip_invited = bool(params.get(\"skip_invited\", True))")
    call = src[i:i + 300]
    assert "if _skip_invited else set()" in call, (
        "чтение журнала приглашений перестало зависеть от выбора владельца")
