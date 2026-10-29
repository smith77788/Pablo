"""Возврат операции в очередь — решение о судьбе: потеря записи не бывает тихой.

МЕХАНИКА. Поллер ставит операции `status='running'` ДО запуска задачи. Поэтому
каждый путь, который выходит из прогона, не сделав работу, обязан вернуть
операцию в очередь сам: цепь открыта, флот занят, Telegram попросил паузу,
операция не уложилась в потолок прогона, занят семафор владельца. Если этот
UPDATE не доехал, операция остаётся 'running' — держит слот параллельности
владельца и блокирует его другие операции до сторожа зависших, то есть ЧАС.
Ровно это каждая из пяти функций и написана предотвращать: см. докстринги
`_release_op_for_circuit` («держа фантомный слот … хотя cooldown цепи всего
30 мин») и `_release_op_for_owner_limit` («значило бы оставить фантомный слот
до сторожа зависших, то есть на час»).

ЧТО БЫЛО НЕ ТАК. Пять из шести таких записей шли через `_safe_execute`: одна
попытка, `log.warning`, и ни одного счётчика. Блип к БД в этот момент
восстанавливал тот самый часовой фантомный слот — молча, невидимо на графике.
Шестая, `_maybe_requeue`, уже ходила через `_retrying_execute` — дверь,
написанную именно под это («Записать РЕШЕНИЕ о судьбе операции: один повтор,
затем громко»). Отличие двух дверей и есть предмет этой переписи.

И отдельно — счётчик, который врал. `_defer_op_for_flood` и
`_resume_op_over_ceiling` поднимали свои счётчики успеха СРАЗУ после записи, не
посмотрев на её результат. Потерянная запись считалась состоявшейся отсрочкой:
на графике отсрочка есть, в базе операция стоит 'running'. Счётчик обязан
считать факты, иначе по нему нельзя судить о системе.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(ROOT, "services", "op_worker.py")

# Дверь, написанная под решения о судьбе операции: повтор, затем громко.
DECISION_DOOR = "_retrying_execute"

# Возвраты в очередь, которые делает САМ прогон, выходя из себя. Сторожа
# зависших и shutdown сюда не входят — у них другая форма (см. самопроверки).
RELEASE_PATHS = (
    "_release_op_for_circuit",
    "_requeue_op_no_accounts",
    "_defer_op_for_flood",
    "_resume_op_over_ceiling",
    "_release_op_for_owner_limit",
    "_maybe_requeue",
)


def _src() -> str:
    with open(SRC_PATH, encoding="utf-8") as f:
        return f.read()


def _tree_and_lines():
    src = _src()
    return ast.parse(src), src.splitlines()


def _enclosing_fn(tree, lineno: str) -> str:
    best, best_span = "?", None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if f.lineno <= lineno <= (f.end_lineno or f.lineno):
                span = (f.end_lineno or f.lineno) - f.lineno
                if best_span is None or span < best_span:
                    best, best_span = f.name, span
    return best


def _writes_pending(sql: str) -> bool:
    """Запись, ставящая операцию в 'pending' — и именно ЗАПИСЬ.

    Первая версия спрашивала только про `status='pending'` и охранника, и
    поймала `_watchdog_alerts`: тот ВЫБИРАЕТ ожидающие операции, чтобы
    предупредить владельца о застрявших, и в его SELECT есть и то и другое.
    Требовать от чтения дверь решений бессмысленно — площадь поиска была шире
    смысла проверки (третья форма сломанного измерителя в CLAUDE.md).
    """
    if not re.search(r"status\s*=\s*'pending'", sql):
        return False
    return bool(re.search(r"\bUPDATE\b", sql) and re.search(r"\bSET\b", sql))


def _queue_returns():
    """Записи, возвращающие операцию в очередь СВОИМ решением.

    Отбор по ДВУМ признакам, и второй здесь несущий: `status='pending'` плюс
    охранник терминальных статусов. Охранник и отличает решение прогона о себе
    от ревизии сторожа: сторож ищет зависшие 'running' и терминальные строки
    ему не грозят, поэтому охранника у него нет. Без второго признака перепись
    захватила бы сторожей и shutdown — у них другая цена потери (их страхует
    следующий круг сторожа), и требовать от них ту же дверь значит мерить
    площадь шире смысла.
    """
    tree, _ = _tree_and_lines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        door = ast.unparse(node.func).split(".")[-1]
        sql = " ".join(ast.unparse(a) for a in node.args[:3])
        if not _writes_pending(sql):
            continue
        if "sql_terminal_list" not in sql and "NOT IN" not in sql:
            continue
        out.append((_enclosing_fn(tree, node.lineno), door, node.lineno))
    return out


# ── Самопроверки отбора: мерим измеритель на заведомо известном ──────────────

def test_the_census_finds_the_healthy_example():
    """`_maybe_requeue` — здоровый образец: он эту дверь и выбрал осознанно."""
    found = {fn: door for fn, door, _ in _queue_returns()}
    assert "_maybe_requeue" in found, (
        "перепись не видит `_maybe_requeue` — отбор сломан, а не код чист")
    assert found["_maybe_requeue"] == DECISION_DOOR


def test_the_census_does_not_reach_the_watchdogs():
    """Сторож и shutdown возвращают в очередь ЧУЖИЕ строки — у них своя цена."""
    found = {fn for fn, _, _ in _queue_returns()}
    for spared in ("_reset_stale_running", "_watchdog_stale", "shutdown"):
        assert spared not in found, (
            f"перепись дотянулась до `{spared}` — площадь поиска шире смысла: "
            "ревизию сторожа страхует его следующий круг, а не повтор записи")


def test_the_census_does_not_reach_a_reader():
    """SELECT ожидающих операций — не решение о судьбе, и двери решений не ждёт."""
    found = {fn for fn, _, _ in _queue_returns()}
    assert "_watchdog_alerts" not in found, (
        "перепись поймала чтение (`_watchdog_alerts` выбирает ожидающие, "
        "чтобы предупредить о застрявших) — она спрашивает про запись")
    assert not _writes_pending(
        "SELECT id FROM operation_queue WHERE status='pending' "
        "AND status NOT IN ('done')"), "отбор не отличает SELECT от UPDATE"
    assert _writes_pending(
        "UPDATE operation_queue SET status='pending' WHERE id=$1"), (
        "отбор перестал видеть саму запись — проверять станет нечего")


def test_the_census_sees_every_release_path():
    """Если путь возврата перестал находиться — проверять станет нечего."""
    found = {fn for fn, _, _ in _queue_returns()}
    missing = [p for p in RELEASE_PATHS if p not in found]
    assert not missing, (
        f"перепись потеряла пути возврата: {missing} — скорее всего, запись "
        "переехала или потеряла охранника терминальных статусов")


# ── Собственно свойство ──────────────────────────────────────────────────────

def test_every_queue_return_goes_through_the_decision_door():
    bad = [(fn, door, ln) for fn, door, ln in _queue_returns()
           if door != DECISION_DOOR]
    assert not bad, (
        "возврат операции в очередь идёт мимо двери решений "
        f"({DECISION_DOOR}): {bad}. Одна попытка — и потерянная запись "
        "оставляет операцию в 'running' на час, молча и без счётчика")


def test_every_queue_return_names_what_is_lost():
    """`where=` различает площадки на графике, `lost=` объясняет потерю в логе."""
    tree, _ = _tree_and_lines()
    thin = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func).split(".")[-1] != DECISION_DOOR:
            continue
        sql = " ".join(ast.unparse(a) for a in node.args[:3])
        if not re.search(r"status\s*=\s*'pending'", sql):
            continue
        kw = {k.arg for k in node.keywords}
        if "where" not in kw:
            thin.append((_enclosing_fn(tree, node.lineno), node.lineno))
    assert not thin, (
        f"возврат в очередь без `where=`: {thin} — на графике все потери "
        "сольются в одну площадку и причину будет не отличить")


# ── Счётчик успеха считает факты, а не попытки ───────────────────────────────

@pytest.mark.parametrize("fn_name, counter", [
    ("_defer_op_for_flood", "infragram_op_flood_defers_total"),
    ("_resume_op_over_ceiling", "infragram_op_ceiling_resumes_total"),
])
def test_the_success_counter_waits_for_the_write(fn_name, counter):
    """Отсрочка, которая не записалась, не состоялась — и считаться не должна."""
    src = _src()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == fn_name)
    body = "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    assert counter in body, f"{fn_name} больше не поднимает {counter}"
    # Счётчик обязан стоять под проверкой результата записи, а не следом за ней.
    assert re.search(r'"ERROR"', body), (
        f"{fn_name} поднимает {counter}, не посмотрев на результат записи — "
        "потерянная запись попадёт на график как состоявшаяся отсрочка")


# ── Поведение на отказавшей БД ───────────────────────────────────────────────

class _FailingPool:
    """Пул, у которого запись не проходит никогда."""

    def __init__(self):
        self.attempts = 0

    async def execute(self, query, *args):
        self.attempts += 1
        raise RuntimeError("db down")

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []


@pytest.fixture
def _quiet(monkeypatch):
    """Счётчики и сообщения владельцу — под наблюдением, в сеть ничего не идёт."""
    from services import op_worker

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(op_worker, "_reliability_metric",
                        lambda name, value=1.0, **lab: seen.append((name, lab)))

    async def _no_notify(*a, **kw):
        return None

    monkeypatch.setattr(op_worker, "_notify_owner_about_op", _no_notify)
    monkeypatch.setattr(op_worker, "report_error", lambda *a, **kw: None)
    return seen


def _names(seen) -> list[str]:
    return [n for n, _ in seen]


def test_a_lost_circuit_release_is_retried_and_counted(_quiet):
    from services import op_worker

    pool = _FailingPool()
    asyncio.run(op_worker._release_op_for_circuit(pool, 42, 1800))
    assert pool.attempts == 2, (
        "потерянный возврат в очередь не повторён — одна попытка оставляет "
        f"операцию в 'running' на час (попыток: {pool.attempts})")
    assert "infragram_requeue_write_failures_total" in _names(_quiet), (
        "потеря возврата не попала ни на один счётчик — на графике её нет")


def test_a_lost_flood_defer_is_not_counted_as_a_defer(_quiet):
    from services import op_worker

    pool = _FailingPool()
    asyncio.run(op_worker._defer_op_for_flood(
        pool, 42, wait_s=7200, reason="flood", bot=None, owner_id=1))
    assert pool.attempts == 2, "потерянная отсрочка не повторена"
    names = _names(_quiet)
    assert "infragram_requeue_write_failures_total" in names, (
        "потеря отсрочки не посчитана")
    assert "infragram_op_flood_defers_total" not in names, (
        "запись не прошла, а счётчик отсрочек поднялся — график показывает "
        "отсрочку, которой не было")


def test_a_lost_ceiling_resume_is_not_counted_as_a_resume(_quiet):
    from services import op_worker

    pool = _FailingPool()
    asyncio.run(op_worker._resume_op_over_ceiling(
        pool, 42, bot=None, owner_id=1, done_so_far=10, total_items=50))
    assert pool.attempts == 2, "потерянное продолжение не повторено"
    names = _names(_quiet)
    assert "infragram_requeue_write_failures_total" in names
    assert "infragram_op_ceiling_resumes_total" not in names, (
        "запись не прошла, а счётчик продолжений поднялся")


def test_a_lost_owner_slot_release_is_retried_and_counted(_quiet):
    from services import op_worker

    pool = _FailingPool()
    asyncio.run(op_worker._release_op_for_owner_limit(pool, 42))
    assert pool.attempts == 2, "потерянный возврат по семафору не повторён"
    assert "infragram_requeue_write_failures_total" in _names(_quiet)


def test_a_lost_fleet_requeue_is_retried_and_counted(_quiet):
    from services import op_worker

    pool = _FailingPool()
    asyncio.run(op_worker._requeue_op_no_accounts(
        pool, 42, defer_s=60, bot=None, owner_id=1))
    assert pool.attempts == 2, "потерянный возврат по занятому флоту не повторён"
    assert "infragram_requeue_write_failures_total" in _names(_quiet)


# ── Тот же вопрос обо ВСЕХ записях статуса операции ─────────────────────────
#
# Возврат в очередь — лишь половина класса. Любая запись, меняющая статус
# операции, теряется одинаково: строка остаётся в прежнем состоянии, а код
# дальше рапортует о том, чего в базе нет. Поэтому ратчет на весь класс, а не
# на найденные экземпляры.

# Двери, у которых есть повтор и счётчик потерь.
RETRYING_DOORS = ("_retrying_execute", "_op_closed_by_us")

# Записи, где потерю дешево подбирает СЛЕДУЮЩИЙ круг, — им повтор не нужен.
# Каждая здесь с причиной, и причина одна и та же: механизм самовосстановим.
SELF_REPAIRING = {
    "_reset_stale_running":
        "ревизия при старте: потерянную строку поднимет сторож зависших",
    "_watchdog_stale":
        "сам сторож зависших: его страхует его же следующий круг",
    "_process_pending":
        "захват строки поллером: не захватил — операция осталась 'pending' "
        "и уйдёт в работу на следующем круге",
    "shutdown":
        "возврат при остановке процесса: не записали — поднимет "
        "`_reset_stale_running` при старте",
}


def _status_writes():
    """Все записи, меняющие СТАТУС операции в `operation_queue`."""
    tree, _ = _tree_and_lines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        door = ast.unparse(node.func).split(".")[-1]
        if door not in ("_safe_execute", "_retrying_execute", "execute",
                        "_op_closed_by_us"):
            continue
        sql = " ".join(ast.unparse(a) for a in node.args[:3])
        if not re.search(r"UPDATE\s+operation_queue", sql):
            continue
        if not re.search(r"SET[^\"\']*status\s*=", sql, re.S):
            continue
        out.append((_enclosing_fn(tree, node.lineno), door, node.lineno))
    return out


def test_the_status_census_sees_both_kinds():
    """Самопроверка: в выборке есть и самовосстановимые, и решения о судьбе."""
    found = {fn for fn, _, _ in _status_writes()}
    assert "_run_op_task" in found, (
        "перепись не видит запись финала — отбор сломан, а не код чист")
    for name in SELF_REPAIRING:
        assert name in found, (
            f"`{name}` больше не находится — список исключений устарел, "
            "и ратчет молча ослаб")


def test_every_status_write_is_retried_or_self_repairing():
    bad = [(fn, door, ln) for fn, door, ln in _status_writes()
           if door not in RETRYING_DOORS and fn not in SELF_REPAIRING]
    assert not bad, (
        "запись статуса операции идёт дверью без повтора и счётчика: "
        f"{bad}. Потерянная запись оставляет строку в прежнем состоянии, "
        "а код дальше докладывает о том, чего в базе нет. Если механизм "
        "самовосстановим — впиши его в SELF_REPAIRING с причиной")


def test_a_lost_poisoned_finish_is_retried_and_counted(_quiet, monkeypatch):
    """Итог операции, остановленной по живучести, — тоже финал.

    Строка уже 'failed', и эта запись дописывает ей цифры. Потеряли — операция
    осталась терминальной и ПУСТОЙ (`result IS NULL`): владелец видит исход без
    единой цифры, а сторож сирот отмены трогает только 'cancelled'.
    """
    from services import op_worker

    pool = _FailingPool()
    announced = {}

    async def _counters(*a, **kw):
        return 3, 2

    async def _announce(*a, **kw):
        announced["yes"] = True

    monkeypatch.setattr(op_worker, "_journal_counters", _counters)
    monkeypatch.setattr(op_worker, "_announce_op_outcome", _announce)

    # Строку функция берёт из RETURNING того же UPDATE, что погасил операцию.
    row = {"id": 42, "owner_id": 1, "op_type": "mass_invite", "params": {},
           "done_items": 3, "total_items": 10}
    asyncio.run(op_worker._finish_poisoned_op(
        pool, row, source="watchdog", bot=None))

    assert pool.attempts == 2, (
        f"потерянный итог не повторён (попыток: {pool.attempts})")
    assert "infragram_op_close_write_failures_total" in _names(_quiet), (
        "незаписанный итог не попал на счётчик — потеря невидима")
    assert announced.get("yes"), (
        "сообщение владельцу пропало: операция действительно остановлена, "
        "и промолчать значит оставить её без объяснения")
