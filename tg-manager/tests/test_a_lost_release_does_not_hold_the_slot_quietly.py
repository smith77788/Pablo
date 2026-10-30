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
    "recover":
        "error_recovery: сброс зависшей 'running' → 'pending'; условие на "
        "'running' не даёт поднять закрытую, а потерю подберёт сторож зависших",
    "_queue_recovery":
        "recovery_engine: та же форма, что и выше, и та же страховка",
}

# Записи, которым повтор не нужен потому, что отказ ПАДАЕТ ЗАКРЫТЫМ: потерянная
# запись не оставляет операцию в опасном состоянии, потому что путь её
# отменяет. Записка здесь проверяемая — см. тест ниже, он требует и отмену, и
# исключение, иначе исключение превратилось бы в «мы подумали, что безопасно».
FAILS_CLOSED_ON_LOSS = {
    "enqueue_op_with_approval":
        "гейт подтверждения: не встал — операция отменяется через дверь шины "
        "и наружу идёт ошибка, поэтому без подтверждения она не исполнится",
}

# Записи, где потеря НЕ тихая: владелец видит отказ сразу и нажимает снова.
# Причина другая, чем у самовосстановимых, поэтому и набор другой — иначе
# «исключение» перестаёт что-либо объяснять.
REPORTED_TO_THE_OWNER = {
    "pause_operations":
        "мини-апп возвращает число приостановленных, а сбой — 500: владелец "
        "видит, что пауза не применилась",
    "resume_operations":
        "то же для возобновления",
    "cb_approval_confirm":
        "экран апрува на исключении показывает «Ошибка подтверждения "
        "операции» и не рапортует об успехе",
}


def _sets_a_status(sql: str) -> bool:
    """Присваивание статуса именно в SET-части, а не где угодно в запросе.

    Прежняя версия искала `status=` после слова SET и перебегала через WHERE:
    запрос `SET result=$2 WHERE id=$1 AND status='cancelled'` считался записью
    статуса, хотя статус он только ПРОВЕРЯЕТ (`_close_cancelled_pending` в
    шине). Третья форма сломанного измерителя из CLAUDE.md, в моём же
    храповике: площадь поиска шире смысла. Поэтому WHERE отрезаем.
    """
    if not re.search(r"UPDATE\s+operation_queue", sql):
        return False
    set_part = re.split(r"\bWHERE\b", sql, flags=re.I)[0]
    return bool(re.search(r"status\s*=", set_part))


_TREE_SKIP_DIRS = {"tests", "__pycache__", ".git", "node_modules", "venv",
                   ".venv", "migrations"}


def _status_writes():
    """Все записи, меняющие СТАТУС операции в `operation_queue`, по ВСЕМУ дереву.

    Раньше читался ровно `services/op_worker.py`. Операцию переводят по
    состояниям и другие модули — движки восстановления, слой мини-аппа,
    постановка с подтверждением, экран апрува, — и ровно там, вне поля зрения
    переписи, нашлись две настоящие находки.
    """
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if "operation_queue" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                door = ast.unparse(node.func).split(".")[-1]
                if door not in ("_safe_execute", "_retrying_execute", "execute",
                                "_op_closed_by_us"):
                    continue
                sql = " ".join(ast.unparse(a) for a in node.args[:3])
                if not _sets_a_status(sql):
                    continue
                out.append((rel, _enclosing_fn(tree, node.lineno), door,
                            node.lineno))
    return out


def test_the_status_census_sees_both_kinds():
    """Самопроверка: в выборке есть и самовосстановимые, и решения о судьбе."""
    found = {fn for _, fn, _, _ in _status_writes()}
    assert "_run_op_task" in found, (
        "перепись не видит запись финала — отбор сломан, а не код чист")
    for name in {**SELF_REPAIRING, **REPORTED_TO_THE_OWNER,
                 **FAILS_CLOSED_ON_LOSS}:
        assert name in found, (
            f"`{name}` больше не находится — список исключений устарел, "
            "и ратчет молча ослаб")


def test_the_census_reaches_beyond_the_worker():
    """Самопроверка площади: перепись обязана видеть модули вне воркера."""
    files = {rel for rel, _, _, _ in _status_writes()}
    for rel in (os.path.join("services", "recovery_engine.py"),
                os.path.join("services", "mini_app_operations.py"),
                os.path.join("database", "db.py")):
        assert rel in files, (
            f"перепись не видит {rel}, хотя он переводит операцию по "
            "состояниям — отбор снова сузился до одного файла")


def test_the_census_does_not_count_a_status_only_checked():
    """`SET result=... WHERE status='cancelled'` статус не пишет."""
    assert not _sets_a_status(
        "UPDATE operation_queue SET result=$2::jsonb, acct_wait_since=NULL "
        "WHERE id=$1 AND status='cancelled'"), (
        "перепись считает записью статуса запрос, который его только "
        "проверяет — площадь поиска перебежала через WHERE")
    assert _sets_a_status(
        "UPDATE operation_queue SET status='paused' WHERE id=$1"), (
        "перепись перестала видеть саму запись статуса")


def test_every_status_write_is_retried_or_excused():
    bad = [(rel, fn, door, ln) for rel, fn, door, ln in _status_writes()
           if door not in RETRYING_DOORS
           and fn not in SELF_REPAIRING
           and fn not in REPORTED_TO_THE_OWNER
           and fn not in FAILS_CLOSED_ON_LOSS]
    assert not bad, (
        "запись статуса операции идёт дверью без повтора и счётчика: "
        f"{bad}. Потерянная запись оставляет строку в прежнем состоянии, "
        "а код дальше докладывает о том, чего в базе нет. Если потерю "
        "подбирает следующий круг — впиши в SELF_REPAIRING; если владелец "
        "видит отказ сразу — в REPORTED_TO_THE_OWNER; обе с причиной")


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


def test_the_fail_closed_excuse_is_checkable():
    """Исключение «падает закрытым» обязано быть видно в коде, а не в словаре.

    Записка «это безопасно» без проверки — обещание, которое живёт до первой
    правки. Поэтому у каждого такого пути требуем две вещи: отмену операции и
    исключение наружу. Без отмены операция осталась бы 'pending' и ушла в
    работу; без исключения вызывающий решил бы, что всё получилось.
    """
    with open(os.path.join(ROOT, "database", "db.py"), encoding="utf-8") as fh:
        src = fh.read()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "enqueue_op_with_approval")
    body = "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    assert "_cancel_op_that_lost_its_gate" in body, (
        "гейт подтверждения больше не отменяет операцию при неудаче — "
        "крупная операция уйдёт в работу без подтверждения владельца")
    assert re.search(r"\braise\b", body), (
        "гейт не поднимает ошибку: вызывающий решит, что операция ждёт "
        "подтверждения, хотя она отменена")
    helper = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_cancel_op_that_lost_its_gate")
    htext = "\n".join(src.splitlines()[helper.lineno - 1:helper.end_lineno])
    assert "operation_bus" in htext and "IN_FLIGHT" in htext, (
        "отмена идёт не дверью шины или без расширенного allow: ноль строк на "
        "гейте значит, что поллер мог успеть взять операцию в 'running'")
