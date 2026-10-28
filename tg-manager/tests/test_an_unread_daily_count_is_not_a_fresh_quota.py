"""Регрессия: непрочитанный суточный счёт больше не выдаёт аккаунту норму заново.

РАЗРЫВ. Перед вступлением и выходом исполнитель читает, сколько аккаунт уже
сделал за сутки, и сверяет с дневным лимитом. Лимит существует против бана:
Telegram смотрит на темп, и двадцать первое вступление за сутки с одного
аккаунта — уже сигнатура.

Сбой чтения подставлял НОЛЬ. Ноль читается как «аккаунт сегодня ничего не
делал», то есть лимит на этот прогон выключался и аккаунт получал полную
дневную норму ВТОРОЙ раз. Сбой доносился до владельца счётчиком (это уже было),
но последствие оставалось: защита не работала именно тогда, когда база
подрагивает, то есть на фоне общей деградации.

Теперь неизвестный счёт считается ИСЧЕРПАННЫМ: аккаунт на этом прогоне не
трогаем, операция идёт остальными аккаунтами, а в итоге владелец видит отдельную
причину пропуска. Сутки простоя одного аккаунта дешевле удвоенной нормы.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from services import op_worker

_SITES = (
    ("_exec_bulk_join_inner", "join", "joins_today"),
    ("_exec_bulk_leave", "leave", "leaves_today"),
)


def _fn_node(name: str):
    src = inspect.getsource(op_worker)
    tree = ast.parse(src)
    return src, next(n for n in ast.walk(tree)
                     if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                     and n.name == name)


def _fn_source(name: str) -> str:
    src, node = _fn_node(name)
    return ast.get_source_segment(src, node) or ""


def _handler_of(name: str, var: str) -> str:
    """Обработчик того самого try, который читает суточный счёт.

    Границы берём по AST: окно фиксированной длины (срез на N символов) здесь
    уже промахнулось мимо выросшего комментария, и отрицательная проверка
    «ноль больше не подставляется» стала бы правдой сама собой.
    """
    src, fn = _fn_node(name)
    for t in ast.walk(fn):
        if isinstance(t, ast.Try):
            body = ast.unparse(ast.Module(body=t.body, type_ignores=[]))
            if f"{var} = await pool.fetchval" in body:
                return "\n".join(ast.unparse(h) for h in t.handlers)
    raise AssertionError(f"{name}: чтение суточного счёта {var} не найдено")


@pytest.mark.parametrize("fn,where,var", _SITES)
def test_an_unread_count_skips_the_account(fn, where, var):
    """Не прочитали счёт — аккаунт пропускаем, а не обнуляем ему сутки."""
    handler = _handler_of(fn, var)
    assert f"{var} = 0" not in handler, (
        f"{fn}: сбой чтения снова подставляет ноль — аккаунт получит дневную "
        "норму второй раз, а лимит существует против бана")
    assert "skipped_unknown_limit += 1" in handler, (
        f"{fn}: пропуск по непрочитанному счёту не считается, и владелец не "
        "узнает, почему аккаунт не работал")
    assert ("return" in handler or "continue" in handler), (
        f"{fn}: после непрочитанного счёта исполнитель всё равно идёт работать "
        "этим аккаунтом")
    assert "infragram_daily_limit_read_failures_total" in handler, (
        f"{fn}: сбой чтения суточного счёта перестал быть виден на графике")


@pytest.mark.parametrize("fn,where,var", _SITES)
def test_the_owner_sees_the_reason_in_the_summary(fn, where, var):
    """Пропуск по непрочитанному счёту обязан попасть в итог словами.

    Иначе владелец видит недобор без причины: «вступили 3 из 10» и ни слова о
    том, что семь аккаунтов не тронули и почему.
    """
    body = _fn_source(fn)
    assert "пропущено (счёт за сутки не прочитан)" in body, (
        f"{fn}: причина пропуска не доходит до владельца")
    assert "skipped_by_limit + skipped_unknown_limit" in body, (
        f"{fn}: пропущенные аккаунты не попали в число пропущенных — панель "
        "операций покажет неправду")


@pytest.mark.parametrize("fn,where,var", _SITES)
def test_the_real_limit_still_skips_the_account(fn, where, var):
    """Обычный путь не тронут: достигнутый лимит по-прежнему пропускает аккаунт."""
    body = _fn_source(fn)
    assert f"if ({var} or 0) >= day_limit:" in body
    assert "skipped_by_limit += 1" in body


# ── Поведение на реальном пути, а не только текст исходника ────────────────


class _Pool:
    """Пул, у которого падает ровно чтение суточного счёта."""

    def __init__(self):
        self.queries: list[str] = []

    def _is_daily_count(self, query: str) -> bool:
        return "operation_audit" in query and "INTERVAL '24 hours'" in query

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if self._is_daily_count(query):
            raise ConnectionResetError("БД отвалилась на чтении суточного счёта")
        return 0

    async def execute(self, query, *args):
        self.queries.append(query)
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_the_account_really_is_left_alone(monkeypatch):
    """Реальный путь: вступления не было, а итог называет причину."""
    from services import account_manager

    attempted: list[tuple] = []

    async def _fake_join(session_str, link, _acc=None):
        attempted.append((int(_acc["id"]), link))
        return {"ok": True}

    async def _not_quarantined(pool, acc_id):
        return False

    async def _instant(*a, **kw):
        return None

    async def _zero(*a, **kw):
        return 0.0

    monkeypatch.setattr(account_manager, "join_channel", _fake_join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _instant)
    monkeypatch.setattr(op_worker, "_governed_delay", lambda pool, owner, base: _zero())

    accounts = [{"id": 1, "phone": "+70000000001", "session_str": "s1"}]
    res = await op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555,
        {"links": ["t.me/alpha"], "delay_mode": "fast"}, accounts)

    assert attempted == [], (
        "аккаунт пошёл вступать с неизвестным суточным счётом — именно так "
        "дневная норма удваивается")
    assert res["skipped_accounts"] == 1, (
        f"пропущенный аккаунт не посчитан: {res}")
    assert "счёт за сутки не прочитан" in res["summary"], (
        f"владелец не узнает причину недобора: {res['summary']}")
