"""Щит, не сумевший поставить аккаунт на паузу, не смеет обещать паузу.

РАЗРЫВ. `apply_shield_decision` — это и есть защита: решил «cool» — продлевает
`cooldown_until` на два часа, решил «pause» — снимает `is_active` на
`cool_duration_hours`. Оба запроса стояли в `try ... except Exception` с
`log.debug` и БЕЗ возвращаемого значения. То есть при сбое записи:

* аккаунт, признанный опасным, оставался полностью активным и шёл в следующую
  массовую операцию в полном темпе;
* `_run_cycle` всё равно делал `paused_total += 1` и отправлял владельцу
  «Аккаунт <b>…</b> поставлен на паузу» с риск-скором и вероятностью бана;
* в логах оставался `debug`, которого никто не читает.

Владелец, получив такое сообщение, перестаёт следить за аккаунтом — ровно за
тем, который сейчас сгорит. Это тот же класс, что разобран выше у Anti-Storm
(«защита, которая включилась только в возвращаемом значении»), но дороже:
Anti-Storm обещал сон всему флоту, а здесь обещание адресное и убедительное.

ЧТО СТАЛО. `apply_shield_decision` возвращает, применилось ли решение; сбой
записи — `log.error` и защитный счётчик
`infragram_shield_apply_write_failures_total` (с меткой действия), который
доходит до владельца через `_watchdog_protective_failures`. Сообщение владельцу
ветвится: применилось — прежний текст, не применилось — прямая просьба
остановить аккаунт вручную.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.fixture(autouse=True)
def _fresh_notify_slots():
    """Уведомления идут через общий слой с анти-спамом на 60 с в памяти
    процесса: слот, занятый соседним тестом, глушил бы сообщение этого."""
    from database import db as _db
    _db._notify_cooldown.clear()
    yield
    _db._notify_cooldown.clear()

COUNTER = "infragram_shield_apply_write_failures_total"


@pytest.fixture
def sh():
    from services import account_shield
    yield account_shield


@pytest.fixture(autouse=True)
def _fresh():
    from services import metrics as _m
    _m.reset()
    yield
    _m.reset()


def _counter() -> float:
    from services import metrics as _m
    counters = (_m.snapshot() or {}).get("counters") or {}
    return sum(float(v) for k, v in counters.items()
               if k == COUNTER or k.startswith(COUNTER + "{"))


class _Pool:
    """Пул, у которого падает именно запись (чтение конфигурации работает)."""

    def __init__(self, *, break_write: bool = False):
        self.break_write = break_write
        self.writes: list = []

    async def execute(self, q, *a):
        if self.break_write:
            raise RuntimeError("БД недоступна")
        self.writes.append(q)
        return "UPDATE 1"

    async def fetchrow(self, q, *a):
        if "tg_accounts" in q and "phone" in q:
            return {"phone": "+700", "first_name": "Ы", "username": None}
        return None

    async def fetch(self, q, *a):
        return []


class _Bot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def _decision(sh, action="pause"):
    return sh.ShieldDecision(
        account_id=5, risk_score=0.91, ban_probability=0.77,
        action=action, note="проверка")


# ── Главное: сбой записи виден и считается ───────────────────────────────────

@pytest.mark.parametrize("action", ["pause", "cool"])
def test_a_failed_apply_is_reported_and_counted(sh, action, caplog):
    """Беззвучный сбой здесь означает активный аккаунт, признанный опасным."""
    pool = _Pool(break_write=True)

    async def _go():
        with caplog.at_level(logging.ERROR, logger="services.account_shield"):
            return await sh.apply_shield_decision(pool, _decision(sh, action), 42)

    applied = asyncio.run(_go())

    assert applied is False, (
        f"{action}: запись не прошла, а решение считается применённым"
    )
    assert _counter() >= 1, f"{action}: сбой защиты не посчитан"
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.levelno >= logging.ERROR)
    assert text.strip(), f"{action}: ни строчки ERROR о неприменённой защите"


def test_a_successful_apply_says_so(sh):
    """Самопроверка мерки: на здоровой записи счётчик молчит."""
    pool = _Pool()

    applied = asyncio.run(sh.apply_shield_decision(pool, _decision(sh), 42))

    assert applied is True, "успешная пауза не отмечена применённой"
    assert _counter() == 0, "счётчик сработал на здоровой записи — мерка сломана"
    assert any("is_active" in q for q in pool.writes), (
        f"запрос паузы не ушёл вовсе: {pool.writes}"
    )


# ── Сообщение владельцу обязано совпадать с реальностью ──────────────────────

def test_an_unapplied_pause_asks_the_owner_to_act(sh):
    """Текст «поставлен на паузу» при неприменённой защите — ложь, за которую
    владелец платит аккаунтом."""
    tg = _Bot()
    cfg = sh.ShieldConfig()

    asyncio.run(sh._notify_pause(tg, _Pool(), 42, _decision(sh), cfg, applied=False))

    assert tg.sent, "о неприменённой защите владельцу не сказали вовсе"
    text = tg.sent[0]
    assert "поставлен на паузу" not in text, (
        f"обещана пауза, которой нет: {text!r}"
    )
    assert "вручную" in text, (
        f"владельцу не сказано, что делать самому: {text!r}"
    )


def test_an_applied_pause_keeps_the_old_text(sh):
    """Обратная сторона: удачная пауза обязана выглядеть как раньше."""
    tg = _Bot()
    cfg = sh.ShieldConfig()

    asyncio.run(sh._notify_pause(tg, _Pool(), 42, _decision(sh), cfg, applied=True))

    assert tg.sent and "поставлен на паузу" in tg.sent[0], (
        f"удачная пауза перестала сообщаться понятно: {tg.sent!r}"
    )


# ── Проводка: счётчик обязан дойти до владельца ──────────────────────────────

def test_the_counter_reaches_the_owner():
    from services import op_worker
    assert COUNTER in op_worker._PROTECTIVE_FAILURE_COUNTERS, (
        "счётчик не в списке защитных тревог — владелец о сбое Щита не узнает"
    )


def test_the_cycle_notifies_by_the_real_outcome(sh):
    """Счётчик цикла и сообщение обязаны идти от ФАКТА, а не от решения."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(sh._run_cycle))
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    applies = [n for n in calls
               if ast.unparse(n.func).endswith("apply_shield_decision")]
    assert applies, "в цикле больше нет вызова apply_shield_decision"
    # Результат вызова обязан куда-то записываться, иначе факт потерян.
    assigned = [n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(ast.unparse(c.func).endswith("apply_shield_decision")
                        for c in ast.walk(n) if isinstance(c, ast.Call))]
    assert assigned, (
        "результат apply_shield_decision отброшен — цикл снова сообщает о "
        "паузе, которой могло не быть"
    )
