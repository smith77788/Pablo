"""Алерт «операция застряла» не присылают заново после каждого деплоя.

ЧТО БЫЛО. Дедуп этих алертов держало множество в памяти процесса
(`_alerted_stuck_ops`), и у него было три беды разом:

  * память процесса. Railway перезапускает процесс на каждом деплое — а деплой
    это ровно тот момент, когда операции и застревают, — и админам приходил тот
    же список заново;
  * при переполнении (>500) множество чистилось ЦЕЛИКОМ, то есть на следующем
    тике «свежими» становились все ранее объявленные операции сразу;
  * операция помечалась объявленной ДО отправки. Пустой список админов или
    ошибка доставки теряли алерт НАВСЕГДА — хотя никто его не получил.

ЧТО ТЕПЕРЬ. Персистентное окно на каждую пару «админ + операция»: повтор не
приходит внутри окна, окно переживает рестарт, а алерт, который не удалось
доставить, вернётся на следующем круге, пока операция ещё застрявшая.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func_source(rel: str, name: str) -> str:
    """Исходник одной функции целиком — по границам из AST."""
    src = _read(rel)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"в {rel} нет функции {name}")


class _Pool:
    """Пул, повторяющий контракт INSERT .. ON CONFLICT .. RETURNING."""

    def __init__(self, rows, *, gate_broken=False):
        self.rows = rows
        self.gate_broken = gate_broken
        self.seen: set = set()
        self.asked: list = []

    async def fetch(self, query, *args):
        return list(self.rows)

    async def fetchrow(self, query, *args):
        if "notification_dedup" not in query:
            return None
        if self.gate_broken:
            raise RuntimeError("БД недоступна")
        self.asked.append((args[0], args[1]))
        if (args[0], args[1]) in self.seen:
            return None
        self.seen.add((args[0], args[1]))
        return {"user_id": args[0]}


class _Bot:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.sent: list = []

    async def send_message(self, aid, text, **kw):
        if aid in self.fail_for:
            raise RuntimeError("админ заблокировал бота")
        self.sent.append((aid, text))


def _row(op_id=7, status="pending", age=25.0):
    return {"id": op_id, "op_type": "mass_publish", "status": status,
            "owner_id": 42, "age_min": age}


def _fire(pool, bot, monkeypatch, admins=(1,)):
    import bot.utils.subscription as sub
    monkeypatch.setattr(sub, "_admin_ids", lambda: set(admins))
    op_worker._active_op_ids.clear()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(op_worker._watchdog_alerts(pool, bot))
    finally:
        loop.close()


# ── Повтор и рестарт ─────────────────────────────────────────────────────────

def test_the_same_operation_is_not_announced_twice(monkeypatch):
    pool, bot = _Pool([_row()]), _Bot()
    _fire(pool, bot, monkeypatch)
    _fire(pool, bot, monkeypatch)
    assert len(bot.sent) == 1, "та же застрявшая операция объявлена дважды"


def test_the_window_is_persistent_and_per_admin(monkeypatch):
    pool, bot = _Pool([_row()]), _Bot()
    _fire(pool, bot, monkeypatch, admins=(1, 2))
    assert len(bot.sent) == 2, "второй админ не получил алерт"
    keys = {k for _uid, k in pool.asked}
    assert keys == {"op-stuck-alert:7"}, keys
    admins_asked = {uid for uid, _k in pool.asked}
    assert admins_asked == {1, 2}, (
        "окно общее на всех админов: сбой доставки одному отнимет алерт у "
        "остальных"
    )


def test_the_memory_set_is_gone():
    assert not hasattr(op_worker, "_alerted_stuck_ops"), (
        "дедуп снова живёт в памяти процесса — деплой пришлёт тот же список "
        "заново, а деплой это и есть момент, когда операции застревают"
    )
    # Границы берём у AST, а не окном фиксированной длины: сдвинулся код —
    # окно промахнулось — «искомого нет» стало бы правдой, и отрицательная
    # проверка ниже выключилась бы молча (храповик
    # test_no_silently_disabled_guards ловит ровно это).
    seg = _func_source("services/op_worker.py", "_watchdog_alerts")
    assert "notify_dedup_ok(" in seg, "анти-повтор алертов не персистентный"
    assert ".clear()" not in seg, (
        "множество объявленных чистится целиком — на следующем тике все "
        "ранее объявленные операции станут «свежими» разом"
    )


# ── Алерт, который никому не ушёл, не считается объявленным ──────────────────

def test_no_admins_does_not_consume_the_alert(monkeypatch):
    pool, bot = _Pool([_row()]), _Bot()
    _fire(pool, bot, monkeypatch, admins=())
    assert not pool.asked, (
        "алерт помечен объявленным, хотя админов нет — он потерян навсегда"
    )
    _fire(pool, bot, monkeypatch, admins=(1,))
    assert len(bot.sent) == 1, "алерт потерялся из-за пустого списка админов"


def test_one_blocked_admin_does_not_rob_the_other(monkeypatch):
    pool, bot = _Pool([_row()]), _Bot(fail_for=(1,))
    _fire(pool, bot, monkeypatch, admins=(1, 2))
    assert [aid for aid, _t in bot.sent] == [2], (
        "ошибка доставки одному админу забрала алерт у остальных"
    )


def test_a_broken_gate_does_not_silence_the_alert(monkeypatch):
    pool, bot = _Pool([_row()], gate_broken=True), _Bot()
    _fire(pool, bot, monkeypatch)
    assert len(bot.sent) == 1, (
        "сбой анти-повтора скрыл застрявшую операцию — fail-open потерян"
    )


# ── Текст алерта ─────────────────────────────────────────────────────────────

def test_the_alert_speaks_russian(monkeypatch):
    pool, bot = _Pool([_row(status="running")]), _Bot()
    _fire(pool, bot, monkeypatch)
    _aid, text = bot.sent[0]
    for bad in ("pending", "running", "owner "):
        assert bad not in text, f"в алерте английское слово: {bad}"
    assert "#7" in text and "выполняется" in text
