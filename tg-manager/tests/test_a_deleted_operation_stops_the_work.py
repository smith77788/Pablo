"""Отмена читается одной дверью, и удалённая операция — тоже «стоп».

РАЗРЫВ. Решение «пора ли исполнителю остановиться» читали три независимых
места. `op_worker._is_cancelled` умеет отличать УДАЛЁННУЮ операцию от
отменённой и останавливается в обоих случаях — это починка по жалобе
«исполняются давно удалённые/отменённые операции». Два других читателя
повторяли исходную ошибку:

    if row and row["status"] == "cancelled":   # удалённую НЕ замечает

`fetchrow` по удалённой строке отдаёт `None`, условие ложно, и прогон идёт
дальше. Места — самые дорогие из возможных:

* `strike_engine.staggered_strike` — волны жалоб с ЖИВЫХ аккаунтов по цели,
  которую владелец уже убрал из списка (сам код называет strike самой
  баноопасной операцией);
* `dm_engine.run_campaign` — ЛС РЕАЛЬНЫМ людям по операции, которой нет.
  Там же, строкой выше, тот же случай для удалённой КАМПАНИИ уже учтён и
  разобран в комментарии — для операции условие осталось прежним.

ЧТО ЗАКРЫВАЕМ. Решение переехало в `op_status.stop_requested(row)` — один
источник правды, как и прочая модель состояний. Перепись ниже ЗАКРЫТАЯ: ни
один файл не имеет права снова решать это сам, а исключение требует
написанной причины.

ПАРА К ЭТОМУ ФАЙЛУ. `tests/test_cancel_has_one_door.py` стережёт другую
сторону отмены — кто имеет право ЗАПИСАТЬ статус 'cancelled'. Здесь — кто и
как его ЧИТАЕТ, то есть доходит ли отмена до работающего исполнителя.
"""
from __future__ import annotations

import ast
import asyncio
import functools
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Сама дверь ───────────────────────────────────────────────────────────────

def test_a_deleted_operation_means_stop():
    """Строки нет — владелец убрал операцию, работать по ней больше незачем."""
    from services import op_status

    assert op_status.stop_requested(None) is True


def test_a_cancelled_operation_means_stop():
    from services import op_status

    assert op_status.stop_requested({"status": "cancelled"}) is True
    assert op_status.stop_requested({"status": " CANCELLED "}) is True, (
        "статус обязан приводиться к канону — иначе отмена зависит от регистра")


def test_a_live_operation_does_not_mean_stop():
    from services import op_status

    for st in ("pending", "running", "paused", "done", "partial", "failed"):
        assert op_status.stop_requested({"status": st}) is False, st


def test_an_unreadable_row_does_not_kill_live_work():
    """Сбой собственного зрения — не повод рвать работу: переспросим позже."""
    from services import op_status

    assert op_status.stop_requested({"no_such_column": 1}) is False
    assert op_status.stop_requested(object()) is False


# ── Закрытая перепись читателей ──────────────────────────────────────────────

# Решения об отмене, которые сознательно остались местными. Причина
# обязательна и проверяется.
ALLOWED = {
    ("services/op_worker.py", "_run_op_task"):
        "здесь строка читается через _safe_fetchrow, а он отдаёт None и при "
        "сбое БД тоже. Для ЗАКРЫТИЯ операции это разные вещи: назвать "
        "отменённой доведённую операцию — ложь владельцу, а незакрытую "
        "отмену всё равно подберёт сторож сирот отмены",
}


@functools.lru_cache(maxsize=1)
def _cancel_decisions() -> tuple[tuple[str, int, str], ...]:
    """Все места, где код сам решает «отменена?» по колонке status.

    Ищется именно СРАВНЕНИЕ значения колонки с 'cancelled' — не упоминание
    строки и не SQL-условие, иначе перепись нашла бы десятки здоровых мест
    (детектор, дающий находки в зрелом коде, почти всегда сломан).
    """
    out: list[tuple[str, int, str]] = []
    for base in ("services", "database", "bot"):
        for dirpath, _d, files in os.walk(os.path.join(ROOT, base)):
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, ROOT)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                funcs = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for node in ast.walk(tree):
                    if not _is_cancel_compare(node):
                        continue
                    owner = _innermost(funcs, node.lineno)
                    out.append((rel, node.lineno, owner))
    return tuple(out)


def _is_cancel_compare(node) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    parts = [node.left] + list(node.comparators)
    if "cancelled" not in {p.value for p in parts if isinstance(p, ast.Constant)}:
        return False
    return any(isinstance(p, ast.Subscript)
               and isinstance(p.slice, ast.Constant) and p.slice.value == "status"
               for p in parts)


def _innermost(funcs, lineno: int) -> str:
    best, best_span = "<модуль>", None
    for fn in funcs:
        if fn.lineno <= lineno <= (fn.end_lineno or fn.lineno):
            span = (fn.end_lineno or fn.lineno) - fn.lineno
            # Вложенные колбэки считаем вместе с внешней функцией: имя внешней
            # понятнее в сообщении об ошибке.
            if best_span is None or span > best_span:
                pass
            if best_span is None or span < best_span:
                best, best_span = fn.name, span
    return best


def test_nobody_decides_cancellation_on_their_own():
    found = _cancel_decisions()
    stray = sorted(
        f"{rel}:{line} (в {owner})" for rel, line, owner in found
        if (rel, owner) not in ALLOWED
    )
    assert not stray, (
        "эти места решают «операция отменена?» сами, а не общей дверью "
        "op_status.stop_requested:\n  " + "\n  ".join(stray) +
        "\nУсловие вида `row and row[\"status\"] == 'cancelled'` НЕ замечает "
        "удалённую операцию (fetchrow отдаёт None) — прогон продолжается: "
        "волны жалоб с живых аккаунтов, ЛС реальным людям. Либо дверь, либо "
        "запись в ALLOWED с причиной."
    )


def test_every_allowed_entry_is_real_and_explained():
    """Переименовали функцию — исключение перестаёт покрывать её молча."""
    found = {(rel, owner) for rel, _l, owner in _cancel_decisions()}
    for key, reason in sorted(ALLOWED.items()):
        assert key in found, (
            f"исключение {key} больше ни к чему не относится — уберите его")
        assert len(reason) > 40 and re.search(r"[а-яА-Я]", reason), (
            f"{key}: причина не написана по-русски и по делу: {reason!r}")


def test_the_three_readers_use_the_door():
    """Прямая проверка тех самых мест: перепись выше их сейчас уже не видит."""
    for rel, where in (
        ("services/op_worker.py", "_is_cancelled"),
        ("services/strike_engine.py", "_op_cancelled"),
        ("services/dm_engine.py", "run_campaign"),
    ):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == where), None)
        assert fn is not None, f"{rel}: функция {where} пропала"
        body = "".join(src.splitlines(keepends=True)[fn.lineno - 1:fn.end_lineno])
        assert "stop_requested" in body, (
            f"{rel}:{where} перестал пользоваться общей дверью отмены")


def test_the_census_detector_bites():
    """Самопроверка: на заведомо больном тексте перепись обязана находить место."""
    sick = ast.parse('if row and row["status"] == "cancelled":\n    pass\n')
    assert any(_is_cancel_compare(n) for n in ast.walk(sick))
    # А на здоровом — молчать: ни SQL-строка, ни чужой столбец не находка.
    for ok in ('x = "status=cancelled"',
               'if row["status"] == "paused": pass',
               'if camp["state"] == "cancelled": pass'):
        assert not any(_is_cancel_compare(n) for n in ast.walk(ast.parse(ok))), ok


# ── Поведение: удалённая операция останавливает DM-кампанию ──────────────────

class _Pool:
    """Кампания жива, операции в очереди НЕТ — её удалили во время рассылки."""

    def __init__(self):
        self.executed: list[str] = []

    async def fetchrow(self, sql, *a):
        if "FROM dm_campaigns" in sql:
            return {"id": 7, "owner_id": 500, "status": "running",
                    "name": "кампания", "text_template": "привет",
                    "target_type": "group_members",
                    # Тихие часы выключены: иначе проверка ждёт «утра»
                    # настоящими минутами (93 секунды на прогон).
                    "params": {"quiet_hours": False}}
        if "FROM operation_queue" in sql:
            return None
        return None

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return "UPDATE 1"

    async def fetchval(self, *a, **k):
        return 0

    async def fetch(self, *a, **k):
        return []


async def _no_sleep(*a, **k):
    return None


def test_a_deleted_operation_stops_the_dm_campaign(monkeypatch):
    """Иначе ЛС продолжают уходить живым людям по операции, которой нет."""
    from services import dm_engine, flood_engine, infra_memory

    async def _accounts(pool, owner_id):
        return [{"id": 1, "session_str": "s", "phone": "+1"}]

    async def _targets(pool, campaign):
        return [{"user_id": 4242, "username": "ivan"}]

    async def _no_quarantine(pool, ids):
        return set()

    async def _tripwire(*a, **k):
        raise AssertionError(
            "ЛС ушло по операции, которую владелец удалил — отмена не сработала")

    monkeypatch.setattr(flood_engine, "get_active_accounts", _accounts)
    monkeypatch.setattr(dm_engine, "_get_targets", _targets)
    monkeypatch.setattr(infra_memory, "quarantined_accounts", _no_quarantine)
    monkeypatch.setattr(dm_engine, "send_dm", _tripwire)

    pool = _Pool()
    try:
        asyncio.run(dm_engine.run_campaign(pool, None, 7, op_id=99))
    except AssertionError:
        raise
    except Exception:
        # Дальше цикла кампания идти не должна вовсе; если дошла и упала на
        # заглушке — это тоже провал, его назовёт проверка ниже.
        pass

    assert any("status='paused'" in s for s in pool.executed), (
        "кампания не остановилась на удалённой операции: ни одной записи "
        "о паузе, то есть цикл пошёл дальше — к отправке живым людям")


def test_a_live_operation_does_not_stop_the_campaign(monkeypatch):
    """Самопроверка пробника: без удаления операции он ничего не «находит»."""
    from services import dm_engine, flood_engine, infra_memory

    class _Live(_Pool):
        async def fetchrow(self, sql, *a):
            if "FROM operation_queue" in sql:
                return {"status": "running"}
            return await super().fetchrow(sql, *a)

    reached = []

    async def _accounts(pool, owner_id):
        return [{"id": 1, "session_str": "s", "phone": "+1"}]

    async def _targets(pool, campaign):
        return [{"user_id": 4242, "username": "ivan"}]

    async def _no_quarantine(pool, ids):
        return set()

    async def _send(*a, **k):
        reached.append(1)
        return {"status": "sent"}

    monkeypatch.setattr(flood_engine, "get_active_accounts", _accounts)
    monkeypatch.setattr(dm_engine, "_get_targets", _targets)
    monkeypatch.setattr(infra_memory, "quarantined_accounts", _no_quarantine)
    monkeypatch.setattr(dm_engine, "send_dm", _send)
    # Пауза между получателями — поведение прода, а не предмет этой проверки.
    monkeypatch.setattr(dm_engine.asyncio, "sleep", _no_sleep)

    pool = _Live()
    try:
        asyncio.run(dm_engine.run_campaign(pool, None, 7, op_id=99))
    except Exception:
        pass

    assert not any("status='paused'" in s for s in pool.executed), (
        "живая операция остановила кампанию — дверь отмены срабатывает впустую")
