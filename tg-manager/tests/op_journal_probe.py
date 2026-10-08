"""Чем исполнитель закрывает цель в журнале операции — читается по дереву разбора.

Журнал целей (`operation_log`) — не отчёт, а основание идемпотентности: полтора
десятка исполнителей читают его ПЕРЕД работой (`completed_targets`,
`completed_steps`, `settled_targets`) и пропускают цели, уже закрытые успехом.
Поэтому проверки «пропускаем по тому же ключу, что пишем» и «ветки успеха и
неудачи попадают в журнал» обязаны видеть ВСЕ записи функции.

ЧТО БЫЛО. Каждый такой пробник искал в тексте функции литерал
`INSERT INTO operation_log` и выдирал параметры регуляркой, рассчитанной на
конкретную расстановку переносов (`op_id,` и `idx,` на отдельных строках).
Два независимых слома:

  * запись успеха ушла за единственную дверь `_journal_done` (одна повторная
    попытка на месте и видимая потеря вместо молчаливой, см.
    `tests/test_journal_success_row_is_not_lost.py`) — и пробники перестали
    видеть успех вообще: на `_exec_ai_comment`, где записей три, один объявил
    «записи в журнал не найдены», другой считал литералы и видел одну из трёх;
  * привязка к переносам строк: `op_id, idx, ref,` в одной строке с
    `"VALUES(...)",` регуляркой не находится. То есть переформатирование
    вызова выключало проверку молча — ровно тот класс, против которого в наборе
    стоит `test_no_silently_disabled_guards`.

ЧТО ТЕПЕРЬ. Записи собираются по AST: вызов двери `_journal_done` (её контракт —
статус `ok`) и любой вызов с SQL-строкой `INSERT INTO operation_log`, у которого
столбцы сопоставляются с параметрами через номера `$n`. Переносы строк, порядок
аргументов и способ записи роли не играют. Разобрать запись не удалось —
возвращается `?`, то есть проверка падает, а не зеленеет.
"""
from __future__ import annotations

import ast
import functools
import pathlib
import re
from typing import NamedTuple

_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"

#: Единственная дверь для записи успешно обработанной цели.
JOURNAL_DOOR = "_journal_done"

_COLS = re.compile(r"INSERT INTO operation_log\s*\(([^)]*)\)", re.I)
_VALS = re.compile(r"VALUES\s*\(([^)]*)\)", re.I)
_PLACEHOLDER = re.compile(r"\$(\d+)")

UNKNOWN = "?"


class Write(NamedTuple):
    """Одна запись в журнал: статус и выражения номера шага и ключа цели."""

    status: str
    step: str
    target: str


@functools.lru_cache(maxsize=1)
def source() -> str:
    return _PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _module() -> ast.Module:
    return ast.parse(source())


@functools.lru_cache(maxsize=None)
def function(name: str) -> ast.AST:
    """Узел функции исполнителя. Нет такой — падаем с понятным текстом."""
    for node in ast.walk(_module()):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}: функции нет в services/op_worker.py")


def _called_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return getattr(f, "id", "")


def _sql_position(call: ast.Call) -> tuple[int, str] | tuple[None, None]:
    """Индекс и текст аргумента-SQL, пишущего в журнал (смежные литералы склеены)."""
    for i, arg in enumerate(call.args):
        if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                and "INSERT INTO operation_log" in arg.value):
            return i, arg.value
    return None, None


def _columns(sql: str, params: list[ast.AST]) -> dict[str, str]:
    """Столбец → выражение параметра (или литерал, если он прямо в VALUES)."""
    cols_m, vals_m = _COLS.search(sql), _VALS.search(sql)
    if not cols_m or not vals_m:
        return {}
    cols = [c.strip() for c in cols_m.group(1).split(",")]
    vals = [v.strip() for v in vals_m.group(1).split(",")]
    out: dict[str, str] = {}
    for col, val in zip(cols, vals):
        ph = _PLACEHOLDER.fullmatch(val)
        if ph:
            idx = int(ph.group(1)) - 1
            out[col] = ast.unparse(params[idx]) if 0 <= idx < len(params) else UNKNOWN
        else:
            out[col] = val.strip("'")
    return out


def writes(name: str) -> list[Write]:
    """Все записи в журнал целей внутри функции исполнителя."""
    found: list[Write] = []
    for call in ast.walk(function(name)):
        if not isinstance(call, ast.Call):
            continue
        if _called_name(call) == JOURNAL_DOOR:
            # Контракт двери: _journal_done(pool, op_id, step_num, target[, message])
            # и статус в ней всегда 'ok'.
            args = call.args
            found.append(Write(
                "ok",
                ast.unparse(args[2]) if len(args) > 2 else UNKNOWN,
                ast.unparse(args[3]) if len(args) > 3 else UNKNOWN,
            ))
            continue
        pos, sql = _sql_position(call)
        if pos is None:
            continue
        cols = _columns(sql, list(call.args[pos + 1:]))
        found.append(Write(
            cols.get("status", UNKNOWN),
            cols.get("step_num", UNKNOWN),
            cols.get("target", UNKNOWN),
        ))
    return found
