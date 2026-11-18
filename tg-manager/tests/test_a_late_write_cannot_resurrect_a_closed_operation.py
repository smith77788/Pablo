"""Запоздавшая запись не поднимает закрытую операцию.

ПОЧЕМУ ЭТО ВАЖНО. Путей вернуть операцию в очередь много: предохранитель
освободил слот, не нашлось свободных аккаунтов, Telegram назначил длинную
флуд-паузу, упёрлись в потолок прогона, сработал лимит владельца, сторож снял
зависшую, воркер выключается. Каждый из них пишет
`UPDATE operation_queue SET status='pending' ...`.

Между решением и записью владелец успевает нажать «Отмена». Если у записи нет
условия на текущий статус, она ПОДНИМЕТ отменённую операцию: та снова уйдёт в
работу, и флот сделает то, что человек только что запретил. Для инвайта или
рассылки это необратимо.

Поэтому каждая такая запись обязана нести условие на статус — либо
`AND status NOT IN (терминальные)` (готово рядом:
`op_status.sql_terminal_list()`), либо своё точное `WHERE status='…'`.

СОСЕДНЯЯ ПРОВЕРКА — отбор операции в работу. Там условие тоже на статус, и
сверх него нужен замок строки: два процесса воркера иначе возьмут одну
операцию, то есть СТАРТУЮТ её дважды. В запросе отбора это `FOR UPDATE ...
SKIP LOCKED` плюс перепроверка `status = 'pending'` под замком.

Обе переписи закрытые: берутся ВСЕ записи, меняющие статус операции на
нетерминальный, а не список, который ведётся руками.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")
# `status` не обязан стоять ПЕРВОЙ колонкой в SET: запрос
# «SET started_at=NULL, status='pending'» прежняя версия не находила вовсе, то
# есть самый опасный вид записи (без условия на статус) мог проехать мимо
# переписи из-за порядка колонок. Поэтому ищем присваивание статуса где угодно
# в SET-части, а не сразу за словом SET.
_UPDATES_THE_QUEUE = re.compile(r"UPDATE\s+operation_queue\b", re.I)
_SETS_NON_TERMINAL_COL = re.compile(
    r"status\s*=\s*'(pending|paused|scheduled|running)'", re.I)


def _sets_non_terminal(sql: str) -> bool:
    if not _UPDATES_THE_QUEUE.search(sql):
        return False
    set_part = re.split(r"\bWHERE\b", sql, flags=re.I)[0]
    return bool(_SETS_NON_TERMINAL_COL.search(set_part))


def _src() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _sql_with_markers(node: ast.Call) -> str:
    """SQL вызова; готовые фрагменты-условия заменяются маркером.

    Условие бывает собрано не литералом, а вызовом `op_status.sql_*_list()` —
    без маркера его присутствие не отличить от отсутствия, и перепись считала
    бы находкой самый аккуратный код.
    """
    txt = ""
    for a in node.args:
        for sub in ast.walk(a):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                txt += " " + sub.value
            elif isinstance(sub, ast.Call):
                name = ast.unparse(sub.func)
                if name.endswith(("sql_terminal_list", "sql_in_flight_list")):
                    txt += " __STATUS_GUARD__ "
    return re.sub(r"\s+", " ", txt)


def fate_writes(src: str) -> list[tuple[int, str, bool]]:
    """Записи, ставящие операции нетерминальный статус: (строка, SQL, есть ли условие)."""
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        sql = _sql_with_markers(n)
        if not _sets_non_terminal(sql):
            continue
        where = sql.split("WHERE", 1)[1] if "WHERE" in sql.upper() else ""
        guarded = ("__STATUS_GUARD__" in sql
                   or re.search(r"status\s*(=|IN|NOT\s+IN)", where, re.I) is not None)
        out.append((n.lineno, sql[:70], bool(guarded)))
    return sorted(out)


def test_every_fate_write_checks_the_current_status():
    naked = [(l, s) for l, s, g in fate_writes(_src()) if not g]
    assert not naked, (
        f"запись возвращает операцию в очередь, не глядя на её статус: {naked}. "
        "Владелец успеет нажать «Отмена» между решением и записью, и такая "
        "запись поднимет отменённую операцию — флот сделает то, что человек "
        "запретил. Добавьте AND status NOT IN "
        "{op_status.sql_terminal_list()} или своё условие на статус")


def test_the_census_finds_the_writes_at_all():
    """Перепись, не нашедшая ни одной записи, зелена и бесполезна."""
    found = fate_writes(_src())
    assert len(found) >= 8, (
        f"путей возврата в очередь найдено {len(found)} — разбор сломан")


_SICK = '''
async def f(pool, op_id):
    await pool.execute(
        "UPDATE operation_queue SET status='pending', started_at=NULL WHERE id=$1",
        op_id)
'''
_GUARDED_BY_LITERAL = '''
async def f(pool, op_id):
    await pool.execute(
        "UPDATE operation_queue SET status='pending', started_at=NULL "
        "WHERE id=$1 AND status='running'",
        op_id)
'''
_GUARDED_BY_FRAGMENT = '''
async def f(pool, op_id):
    await pool.execute(
        "UPDATE operation_queue SET status='pending', started_at=NULL WHERE id=$1"
        f" AND status NOT IN {op_status.sql_terminal_list()}",
        op_id)
'''


def test_the_census_bites():
    assert fate_writes(_SICK) and not fate_writes(_SICK)[0][2], (
        "перепись не видит запись без условия на статус")


def test_the_census_accepts_a_literal_guard():
    assert fate_writes(_GUARDED_BY_LITERAL)[0][2]


def test_the_census_accepts_the_shared_fragment():
    assert fate_writes(_GUARDED_BY_FRAGMENT)[0][2], (
        "готовый фрагмент op_status.sql_terminal_list() не признан условием — "
        "перепись потребует правок у самого аккуратного кода")


# ── Отбор в работу: один процесс, одна операция ──────────────────────────────

def test_the_claim_query_cannot_start_an_operation_twice():
    """Два процесса воркера не возьмут одну операцию.

    Условия на статус тут недостаточно: между SELECT и UPDATE второй процесс
    успеет прочитать ту же строку. Нужен замок строки, пропуск занятых и
    перепроверка статуса ПОД замком.
    """
    src = _src()
    claim = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call):
            sql = _sql_with_markers(n)
            if "UPDATE operation_queue SET status = 'running'" in sql \
                    and "picked" in sql:
                claim = sql
                break
    assert claim, "запрос отбора операций в работу не найден — перепись ослепла"
    assert "FOR UPDATE" in claim.upper() and "SKIP LOCKED" in claim.upper(), (
        "в отборе нет замка строки: два процесса воркера возьмут одну операцию")
    assert claim.upper().count("STATUS = 'PENDING'") >= 2, (
        "статус не перепроверяется под замком — отбор полагается на чтение до "
        "блокировки")


# ── Та же проверка по ВСЕМУ дереву, а не по одному файлу ────────────────────
#
# Выше читается `services/op_worker.py`, и имя файла обещает свойство целиком.
# Но операцию возвращают в очередь и другие модули: `services/error_recovery.py`
# и `services/recovery_engine.py` поднимают зависшую своим UPDATE. Сейчас оба
# условие на статус ставят — но ничто их на этом не держало, а цена ошибки
# здесь максимальная: владелец нажал «Отмена», а движок восстановления вернул
# операцию в 'pending' и флот сделал то, что человек запретил.
#
# Тот же урок, что и у переписи отмены: проверка, читающая один файл при имени
# про свойство, — это выбор «кого судить» по списку путей.

_TREE_SKIP_DIRS = {"tests", "__pycache__", ".git", "node_modules", "venv",
                   ".venv", "migrations"}


def _py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def _fate_writes_everywhere():
    out = []
    for path in _py_files():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if "operation_queue" not in src:
            continue
        try:
            found = fate_writes(src)
        except SyntaxError:
            continue
        rel = os.path.relpath(path, ROOT)
        for lineno, sql, guarded in found:
            out.append((rel, lineno, sql, guarded))
    return out


def test_the_tree_census_reaches_the_other_modules():
    """Самопроверка: перепись обязана видеть модули ВНЕ op_worker."""
    seen = {rel for rel, _, _, _ in _fate_writes_everywhere()}
    for rel in (os.path.join("services", "error_recovery.py"),
                os.path.join("services", "recovery_engine.py")):
        assert rel in seen, (
            f"перепись не видит {rel}, хотя он возвращает операцию в очередь — "
            "отбор снова сузился до одного файла")


def test_no_module_resurrects_an_operation_without_looking_at_its_status():
    naked = [(rel, ln, sql) for rel, ln, sql, g in _fate_writes_everywhere()
             if not g]
    assert not naked, (
        f"запись возвращает операцию в очередь, не глядя на её статус: {naked}. "
        "Владелец успеет нажать «Отмена» между решением и записью, и такая "
        "запись поднимет отменённую операцию — флот сделает то, что человек "
        "запретил")


_SICK_COLUMN_ORDER = '''
async def f(pool, op_id):
    await pool.execute(
        "UPDATE operation_queue SET started_at=NULL, status='pending' WHERE id=$1",
        op_id)
'''


def test_the_census_bites_when_status_is_not_the_first_column():
    """Порядок колонок в SET не должен прятать запись от переписи."""
    found = fate_writes(_SICK_COLUMN_ORDER)
    assert found, (
        "запись со статусом НЕ первой колонкой не найдена — порядок колонок "
        "прячет от переписи самый опасный вид записи")
    assert not found[0][2], "...и она должна считаться записью без условия"
