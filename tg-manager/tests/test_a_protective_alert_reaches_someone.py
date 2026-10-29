"""Алерт о сбое защиты должен доходить до админа, вошедшего по секрету.

Весь слой защитных отказов устроен так: операция отказывается делать
необратимое действие, если запись-дверь не легла, и говорит об этом счётчиком,
который `_watchdog_protective_failures` превращает в сообщение админу. То есть
получатель алерта — последнее звено, и если его нет, молчат ВСЕ отказы разом.

Получателей брали из одного места — переменной окружения `ADMIN_IDS`. А
продукт знает про платформенного админа шире: `is_platform_admin` принимает и
того, кто вошёл в админку по `ADMIN_SECRET`, и такие сессии лежат в
`platform_admin_sessions` со сроком, то есть переживают перезапуск. Мини-апп
этих людей админами считает, а оповещения — нет. При незаполненном `ADMIN_IDS`
(а это обычная установка) сообщить было некому вообще.

Теперь получатели у алертов одни: `ADMIN_IDS` плюс живые сессии админки.
Случай «нет вообще никого» остался и по-прежнему считается отдельным
счётчиком: это не сбой защиты, а полный отказ оповещений о ней.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _Pool:
    """Стенд базы: живые сессии админки — то, что вернёт SELECT."""

    def __init__(self, session_admins=()):
        self.session_admins = [int(x) for x in session_admins]

    async def fetch(self, sql, *args):
        if "platform_admin_sessions" in sql:
            return [{"user_id": uid} for uid in self.session_admins]
        return []

    async def execute(self, sql, *args):
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None


class _Bot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((int(chat_id), text))


@pytest.fixture
def grown(monkeypatch):
    """Счётчик защитного отказа вырос — алерту есть о чём говорить."""
    from database import db
    from services import op_worker

    name = next(iter(op_worker._PROTECTIVE_FAILURE_COUNTERS))
    op_worker._reliability_metric(name, where="тест")

    async def _dedup_ok(pool, uid, key, ttl):
        return True

    async def _forget(pool, uid, key):
        return None

    monkeypatch.setattr(db, "notify_dedup_ok", _dedup_ok)
    monkeypatch.setattr(db, "notify_dedup_forget", _forget)
    monkeypatch.setenv("ADMIN_IDS", "")
    return name


def test_a_session_admin_receives_the_alert(grown):
    """ADMIN_IDS пуст, но админка знает вошедшего — алерт уходит ему."""
    from services import op_worker

    bot = _Bot()
    _run(op_worker._watchdog_protective_failures(
        _Pool(session_admins=[4242]), bot, {}))

    assert [uid for uid, _ in bot.sent] == [4242], (
        "алерт не ушёл админу, вошедшему по секрету: при незаполненном "
        "ADMIN_IDS о сбое защиты не узнаёт никто")
    assert "Защита не записалась" in bot.sent[0][1], bot.sent[0][1]


def test_nobody_at_all_is_still_counted(grown):
    """Нет ни переменной, ни сессий — это отдельный, считаемый отказ."""
    from services import op_worker

    before = _total("infragram_protective_alerts_no_recipient_total")
    bot = _Bot()
    seen: dict = {}
    _run(op_worker._watchdog_protective_failures(_Pool(), bot, seen))

    assert bot.sent == [], bot.sent
    assert _total("infragram_protective_alerts_no_recipient_total") > before, (
        "полный отказ оповещений о защите виден только в логе воркера")
    assert seen == {}, (
        "база отсчёта двинулась, хотя сообщение никому не ушло — на следующем "
        "круге алерт не повторится")


def test_a_stuck_operation_alert_has_the_same_recipients():
    """У алерта о застрявших операциях получатели те же."""
    from services import op_worker

    src = ""
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    for name in ("_watchdog_alerts", "_watchdog_protective_failures"):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
                body = ast.unparse(node)
                assert "platform_alert_recipients" in body, (
                    f"{name} берёт получателей не из общего места: список "
                    "окажется уже того, кого продукт считает админом")
                break


def test_no_alert_path_narrows_the_admin_list():
    """Храповик: в слое операций получателей алертов берут одним способом."""
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    bare = [i for i, line in enumerate(src.splitlines(), 1)
            if "_admin_ids()" in line]
    assert bare == [], (
        "кто-то снова резолвит получателей через _admin_ids(): этот список "
        f"не включает вошедших в админку по секрету — строки {bare}")
