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


# --- Тот же класс по всему коду, а не в одном файле -------------------------
#
# Храповик выше сторожит слой операций. Но резолвить получателей самому можно
# откуда угодно, и так оно и было: `services/payment_checker.py` брал админов
# тем же старым `_admin_ids()`, то есть алерт о платежах при незаполненном
# `ADMIN_IDS` не уходил никому. Правило ниже считает класс по всему коду.
#
# Законных мест для `_admin_ids()` ровно два, и они разные по смыслу:
#   * дом самого помощника (`bot/utils/subscription.py`) — он на нём и стоит;
#   * проверка ПРАВ (`_is_admin`): там узкий список безопасен, потому что
#     ошибка в эту сторону отказывает в доступе, а не раздаёт его. Такие
#     проверки и так смотрят сессии админки отдельной ветвью.
# Всё остальное — это оповещение, и получателей ему брать из общего места.
_ADMIN_IDS_HOME = ("bot/utils/subscription.py",)


def _enclosing_function(tree, lineno: int) -> str:
    best, best_line = "", -1
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.lineno <= lineno <= (node.end_lineno or node.lineno)
                and node.lineno > best_line):
            best, best_line = node.name, node.lineno
    return best


def _bare_admin_id_calls() -> list[str]:
    """Вызовы `_admin_ids()` по ДЕРЕВУ, а не по тексту строки.

    Поиск подстроки считал находкой строку `def _admin_ids() -> set[int]:` —
    то есть само объявление помощника. Тот же урок, что и в соседнем треде, где
    подстрочный поиск пауз дал двадцать восемь находок вместо тринадцати,
    потому что в слове `await` сидит `wait`: имена берутся по дереву.

    И уже по дереву правило ошиблось второй раз — на однофамильце: у охраны
    чата свой `_admin_ids(bot, chat_id)`, это админы телеграм-чата. Поэтому
    считаются только вызовы БЕЗ аргументов.
    """
    import pathlib

    root = pathlib.Path(ROOT)
    out = []
    for path in (sorted((root / "services").rglob("*.py"))
                 + sorted((root / "bot").rglob("*.py"))):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if rel in _ADMIN_IDS_HOME:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "_admin_ids" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # БЕЗ АРГУМЕНТОВ: в `bot/handlers/chat_guard.py` живёт свой
            # `_admin_ids(bot, chat_id)` — админы ТЕЛЕГРАМ-ЧАТА, которых не
            # модерируют. С платформенными получателями он не имеет общего
            # ничего, кроме имени, и правило, судившее по имени, выдало на нём
            # ложную находку. Платформенный помощник аргументов не берёт.
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_admin_ids"
                    and not node.args and not node.keywords):
                continue
            fn = _enclosing_function(tree, node.lineno)
            if fn.startswith("_is_admin") or fn.startswith("is_"):
                continue
            out.append(f"{rel}:{node.lineno} в {fn or '<модуль>'}")
    return out


def test_the_rule_knows_where_admin_ids_may_still_be_read():
    """Проба: законные места существуют, иначе правило ниже бессмысленно."""
    import pathlib

    home = pathlib.Path(ROOT) / _ADMIN_IDS_HOME[0]
    assert "_admin_ids()" in home.read_text(encoding="utf-8"), (
        "общий помощник больше не стоит на _admin_ids — правило устарело")
    tree = ast.parse("def _is_admin(u):\n    return u in _admin_ids()\n")
    assert _enclosing_function(tree, 2) == "_is_admin"


def test_only_rights_checks_resolve_admins_themselves():
    bad = _bare_admin_id_calls()
    assert not bad, (
        "получателей оповещения резолвят своим списком: он не включает "
        "вошедших в админку по секрету, и при незаполненном ADMIN_IDS "
        "сообщить будет некому. Нужен platform_alert_recipients(pool):\n  "
        + "\n  ".join(bad))
