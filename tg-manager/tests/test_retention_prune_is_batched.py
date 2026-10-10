"""Уборка старых данных обязана удалять пачками, а не одним запросом.

Что было. `services/db_maintenance.py` чистил каждую журнальную таблицу одним
безлимитным запросом:

    WITH d AS (DELETE FROM behavioral_events
               WHERE occurred_at < NOW() - INTERVAL '90 days' RETURNING 1)
    SELECT COUNT(*) FROM d

На пустой базе это работает. На выросшей — нет: одна транзакция на миллионы
строк держит блокировки, раздувает WAL и почти наверняка не доживает до конца
(таймаут запроса, редеплой Railway, рестарт контейнера). Postgres откатывает
её ЦЕЛИКОМ — не удаляется ни одной строки. Следующий проход через шесть часов
начинает с того же места и падает так же, и так бессрочно: таблица растёт, а
в логе только `failed to prune` раз в шесть часов. Снаружи уборка выглядит
рабочей, пока база не упирается в диск.

Чем больше таблица, тем вернее откат — то есть защита отключается ровно тогда,
когда она нужна.

Тест держит форму запроса: у каждого DELETE есть LIMIT, и потолок на проход
конечен. Он падает на старом коде (LIMIT там не было ни в одном запросе).
"""
from __future__ import annotations

import ast
import os

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "services",
    "db_maintenance.py",
)


def _sql_literals(src: str) -> list[str]:
    """Все строковые константы модуля, включая f-строки (по литеральным кускам).

    Куски f-строки собираются в одну строку: подстановки ({table}) выпадают, а
    SQL вокруг них остаётся целым. Внутрь f-строки повторно не спускаемся —
    иначе каждый её кусок пришёл бы ещё и отдельной «строкой».
    """
    tree = ast.parse(src)
    inner = {
        id(v)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for v in node.values
    }
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inner
        ):
            out.append(node.value)
    return out


def _delete_statements(src: str) -> list[str]:
    return [s for s in _sql_literals(src) if "DELETE FROM" in s.upper()]


def test_every_delete_is_limited():
    src = open(_SRC, encoding="utf-8").read()
    stmts = _delete_statements(src)
    assert stmts, "в db_maintenance не нашлось ни одного DELETE — тест устарел"

    for sql in stmts:
        upper = sql.upper()
        assert "LIMIT" in upper, (
            "безлимитный DELETE в уборке:\n"
            f"{sql}\n"
            "одна транзакция на всю таблицу откатится на большом объёме — "
            "не удалится ничего, и так каждый проход"
        )
        assert "CTID" in upper, (
            "DELETE ограничен не по ctid:\n"
            f"{sql}\n"
            "у DELETE в Postgres нет LIMIT — пачка отбирается подзапросом по ctid"
        )


def test_batch_ceiling_is_finite():
    from services import db_maintenance as dm

    assert 0 < dm._BATCH <= 50_000, "размер пачки должен быть небольшим и конечным"
    assert dm._MAX_PER_TABLE >= dm._BATCH, "потолок прохода меньше одной пачки"
    assert dm._BATCH_PAUSE >= 0, "пауза между пачками не может быть отрицательной"


def test_batch_loop_cannot_spin_forever():
    """Цикл пачек обязан иметь верхнюю границу — иначе уборка не отпустит пул."""
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_prune_batched"
    )
    whiles = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert whiles, "_prune_batched больше не цикл — тест устарел"
    for w in whiles:
        assert not (
            isinstance(w.test, ast.Constant) and w.test.value is True
        ), "while True в удалении пачками: проход может не кончиться никогда"


# ── поведение цикла на живом вызове (не только форма запроса) ────────────────


class _FakePool:
    """Пул, отдающий заранее заданные ответы на fetchval; помнит SQL каждой пачки."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.calls: list[str] = []

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        a = self._answers.pop(0) if self._answers else 0
        if isinstance(a, Exception):
            raise a
        return a


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_loop_continues_until_short_batch(monkeypatch):
    from services import db_maintenance as dm

    monkeypatch.setattr(dm, "_BATCH", 10)
    monkeypatch.setattr(dm, "_BATCH_PAUSE", 0)
    pool = _FakePool([10, 10, 3])

    total, err = _run(dm._prune_batched(pool, "behavioral_events", "occurred_at < NOW()"))

    assert err is None
    assert total == 23, "неполная пачка означает конец, а не остановку раньше времени"
    assert len(pool.calls) == 3


def test_partial_progress_survives_failure(monkeypatch):
    """Упавшая пачка не отменяет уже удалённые: их число возвращается честно."""
    from services import db_maintenance as dm

    monkeypatch.setattr(dm, "_BATCH", 10)
    monkeypatch.setattr(dm, "_BATCH_PAUSE", 0)
    pool = _FakePool([10, RuntimeError("connection reset")])

    total, err = _run(dm._prune_batched(pool, "operation_log", "created_at < NOW()"))

    assert isinstance(err, RuntimeError)
    assert total == 10, "первая пачка зафиксирована — её нельзя считать неудалённой"


def test_run_stops_at_ceiling(monkeypatch):
    """Бесконечный хвост не держит соединение: проход упирается в потолок."""
    from services import db_maintenance as dm

    monkeypatch.setattr(dm, "_BATCH", 10)
    monkeypatch.setattr(dm, "_MAX_PER_TABLE", 35)
    monkeypatch.setattr(dm, "_BATCH_PAUSE", 0)
    pool = _FakePool([10] * 100)

    total, err = _run(dm._prune_batched(pool, "search_snapshots", "captured_at < NOW()"))

    assert err is None
    assert total == 40, "цикл обязан остановиться сразу за потолком"
    assert len(pool.calls) == 4
