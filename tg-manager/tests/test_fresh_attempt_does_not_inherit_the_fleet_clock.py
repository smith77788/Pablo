"""Поднятая заново операция не наследует чужое ожидание флота.

ЧТО БЫЛО. `acct_wait_since` — часы ожидания свободных аккаунтов. Ставит их
первое откладывание из-за занятого флота, и через `_ACCT_WAIT_MAX_MIN` (20 мин)
операция закрывается как «не дождались свободных аккаунтов за N мин». Для этого
они и нужны: иначе операция ждала бы флот вечно.

Но `_maybe_retry_op` снимал эти часы на новой попытке (с явным объяснением
почему), а ПЯТЬ остальных путей возврата операции в очередь — нет:

  * освобождение после кулдауна предохранителя;
  * `_reset_stale_running` — подъём осиротевших строк после рестарта воркера;
  * `_watchdog_stale` — сторож зависших;
  * возврат очереди при остановке воркера («подхватит с чистого листа»);
  * `recovery_engine._queue_recovery` — возобновление зависшей операции.

Все пять — это НОВАЯ попытка сделать работу, а не продолжение ожидания флота.
Операция, однажды подождавшая аккаунты полчаса, после рестарта процесса
закрывалась на ПЕРВОЙ же встрече с занятым флотом текстом «не дождались
свободных аккаунтов за 187 мин» — хотя в этой попытке не ждала нисколько, а
187 минут — время с прошлого раза. Ровно тот симптом, от которого часы и
вводили: отложенная операция, упавшая без единой попытки.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import pathlib

import pytest

from services import op_worker

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Pool:
    def __init__(self, waiting_since):
        self.waiting_since = waiting_since
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        if "acct_wait_since" in query and "SELECT" in query:
            return {"acct_wait_since": self.waiting_since}
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def test_clean_clock_defers_instead_of_closing(monkeypatch):
    """Часы сняты — операция ждёт флот заново, а не падает мгновенно."""
    closed: list[int] = []

    async def _no_close(*a, **kw):
        closed.append(kw.get("waited_min", -1))

    monkeypatch.setattr(op_worker, "_finish_fleet_starved_op", _no_close)
    pool = _Pool(None)
    asyncio.run(op_worker._requeue_op_no_accounts(pool, 8001))

    assert closed == [], "операция с чистыми часами закрыта как «не дождалась»"
    assert any("acct_wait_since = COALESCE" in q for q, _ in pool.executed), (
        "отметку ожидания ставит не первое откладывание — предел ожидания "
        "не наступит никогда")


def test_stale_clock_closes_the_operation_instantly(monkeypatch):
    """Цена ненулевых часов: закрытие на первой же встрече с занятым флотом."""
    closed: list[int] = []

    async def _close(pool, op_id, owner_id, waited_min=0, bot=None):
        closed.append(waited_min)

    monkeypatch.setattr(op_worker, "_finish_fleet_starved_op", _close)
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=187)
    asyncio.run(op_worker._requeue_op_no_accounts(_Pool(long_ago), 8002))

    assert closed and closed[0] >= 180, (
        "именно так выглядела ложь: «не дождались свободных аккаунтов за 187 "
        f"мин» на попытке, которая не ждала нисколько — {closed}")


# ── Храповик: ни один путь «новой попытки» не забывает снять часы ────────────

_FILES = ("services/op_worker.py", "services/recovery_engine.py",
          "services/mini_app_api.py")

# Два пути, которым часы снимать НЕЛЬЗЯ:
#   * `_requeue_op_no_accounts` — само ожидание флота, оно этими часами и живёт;
#   * `_release_op_for_owner_limit` — место в семафоре владельца заняли ЕГО ЖЕ
#     операции, то есть «флот занят другими операциями» здесь правда, и сброс
#     часов на каждом таком возврате (20 секунд) снял бы предел ожидания совсем:
#     операция ходила бы по кругу между занятым семафором и занятым флотом без
#     выхода.
_KEEPS_THE_CLOCK = ("_requeue_op_no_accounts", "_release_op_for_owner_limit")


def _requeue_statements(rel: str):
    """(функция, текст SQL) для каждого запроса, переводящего операцию в 'pending'.

    Границы — один вызов БД (узел Call), а не окно фиксированной длины и не вся
    функция: иначе `_exec_global_presence_bot`, который трогает operation_queue и
    отдельно ставит 'pending' в СВОЕЙ таблице целей, попал бы в список ни за что.
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for call in ast.walk(func):
            if not isinstance(call, ast.Call):
                continue
            parts: list[str] = []
            for node in ast.walk(call):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    parts.append(node.value)
            flat = " ".join(" ".join(parts).split())
            if "operation_queue" not in flat:
                continue
            if ("SET status='pending'" in flat
                    or "SET status = 'pending'" in flat):
                yield func.name, flat


def test_detector_sees_the_known_paths():
    """Самопроверка измерителя: он обязан находить сами эти пути."""
    found = {name for rel in _FILES for name, _ in _requeue_statements(rel)}
    assert "_requeue_op_no_accounts" in found, (
        "измеритель не видит даже путь ожидания флота — он сломан, "
        f"а не код: {sorted(found)}")
    assert len(found) >= 5, f"найдено подозрительно мало путей: {sorted(found)}"
    assert "_exec_global_presence_bot" not in found, (
        "измеритель считает требованием чужую таблицу целей")


def test_every_requeue_path_handles_the_fleet_clock():
    offenders = []
    for rel in _FILES:
        for name, flat in _requeue_statements(rel):
            if name in _KEEPS_THE_CLOCK:
                continue
            if "acct_wait_since" not in flat:
                offenders.append(f"{rel}:{name}")
    assert not offenders, (
        "путь возвращает операцию в очередь, не тронув часы ожидания флота: "
        "новая попытка унаследует чужое ожидание и закроется словами «не "
        "дождались свободных аккаунтов за N мин» без единой попытки:\n"
        + "\n".join(offenders))
