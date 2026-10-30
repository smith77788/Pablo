"""Отметка «сделано» не пишется молча.

ЧТО СТОИТ НА КОНУ. Журнал целей — источник правды о том, что уже сделано: по
нему двери идемпотентности решают, какие цели пропустить на возобновлённом
прогоне. Пропавшая отметка `ok` не оставляет следа, и следующий прогон делает
цель ВТОРОЙ раз: второе сообщение живому человеку, второй пост в канал, второе
приглашение. Повтор прогона — не редкость: так работает `_maybe_requeue` после
сетевого сбоя, так сбрасывает зависшую операцию сторож, так поднимает
незавершённые операции старт воркера после каждого деплоя.

Поэтому отметки пишутся не обычным `_safe_execute` (он пишет warning и
возвращает «ERROR»), а громкими писателями: `_journal_write`, `_journal_done`
и `_journal_writemany` — повтор на месте, затем `log.error` и счётчик
`infragram_journal_write_failures_total`, чтобы риск дубля был виден на
графике.

ЧТО НАШЛОСЬ. Громких писателей слушались 49 вставок из 58. Семь отметок `ok`
писались прямым `pool.execute`, и худшая — в массовом инвайте: успешные цели
батчем, в `try/except`, который исключение ГАСИЛ. То есть сбой базы на этой
строке означал: людей пригласили, отметок нет, повтор приглашает их снова — в
самой баноопасной операции продукта. Та же отметка кормит экран ретеншена
(`status='ok' AND message='joined'`), так что её потеря ещё и обнуляла приток.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Писатели, которые НЕ теряют отметку молча: повтор, затем log.error и счётчик.
LOUD_WRITERS = ("_journal_write", "_journal_writemany", "_journal_done")
# Их собственные тела — то самое место, где прямой pool.execute и должен быть.
_WRITER_BODIES = ("_journal_write", "_journal_writemany", "_journal_done")


def _src() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def quiet_journal_writes(src: str) -> list[tuple[int, str]]:
    """Вставки в журнал целей мимо громких писателей: (строка, чем пишут)."""
    tree = ast.parse(src)
    inside_writer: set[int] = set()
    for n in tree.body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and n.name in _WRITER_BODIES:
            inside_writer.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if not any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                   and "INSERT INTO operation_log" in a.value for a in n.args):
            continue
        if n.lineno in inside_writer:
            continue
        callee = ast.unparse(n.func)
        if any(callee.endswith(w) for w in LOUD_WRITERS):
            continue
        out.append((n.lineno, callee))
    return sorted(out)


# ── Перепись ─────────────────────────────────────────────────────────────────

def test_no_journal_mark_is_written_quietly():
    quiet = quiet_journal_writes(_src())
    assert not quiet, (
        f"отметки в журнал целей пишутся мимо громких писателей: {quiet}. "
        "Обычный pool.execute на сбое базы теряет отметку БЕЗ следа, и "
        "повторный прогон делает цель второй раз. Пишите через "
        f"{', '.join(LOUD_WRITERS)}")


_SICK = '''
async def f(pool, op_id):
    await pool.execute(
        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
        op_id, 1, "@x")
'''
_HEALTHY = '''
async def f(pool, op_id):
    await _journal_write(
        pool,
        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
        op_id, 1, "@x", where="f")
'''


def test_the_census_bites():
    assert quiet_journal_writes(_SICK), "перепись не видит прямую вставку"


def test_the_census_accepts_the_loud_writer():
    assert not quiet_journal_writes(_HEALTHY), (
        "громкий писатель не признан — перепись потребует правок там, где всё "
        "уже верно")


def test_the_census_sees_the_writes_at_all():
    """Перепись, не нашедшая ни одной вставки, зелена и бесполезна."""
    src = _src()
    assert src.count("INSERT INTO operation_log") > 40, (
        "вставок найдено подозрительно мало — разбор сломан")


# ── Батчевый писатель ────────────────────────────────────────────────────────

class _FailingPool:
    """Пул, у которого executemany всегда падает."""

    def __init__(self) -> None:
        self.tries = 0

    async def executemany(self, q, rows):
        self.tries += 1
        raise RuntimeError("пул занят")

    async def execute(self, q, *a):
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_the_batch_writer_retries_and_then_shouts(monkeypatch):
    from services import op_worker as W

    seen: list[tuple] = []
    monkeypatch.setattr(W, "_reliability_metric",
                        lambda name, **kw: seen.append((name, kw)))

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(W.asyncio, "sleep", _no_sleep)

    pool = _FailingPool()
    res = await W._journal_writemany(
        pool, "INSERT INTO operation_log(op_id, step_num, target, status) "
              "VALUES($1,$2,$3,'ok')",
        [(1, 0, "@x")], where="test")
    assert res == "ERROR", "батчевый писатель скрыл сбой"
    assert pool.tries == 2, f"повтора не было: попыток {pool.tries}"
    assert any(n == "infragram_journal_write_failures_total" for n, _ in seen), (
        f"счётчика потерянной отметки нет: {seen}")


@pytest.mark.asyncio
async def test_the_batch_writer_returns_the_pool_answer():
    from services import op_worker as W

    class _OkPool:
        async def executemany(self, q, rows):
            return None

    res = await W._journal_writemany(_OkPool(), "INSERT INTO operation_log(op_id) "
                                                "VALUES($1)", [(1,)])
    assert res != "ERROR"


# ── Инвайт: отметка успеха и её исход ────────────────────────────────────────

def _fn(name: str) -> str:
    src = _src()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"{name} не найдена")


def test_the_invite_logs_its_successes_loudly():
    body = _fn("_exec_mass_invite")
    assert "_journal_writemany(" in body, (
        "успешные цели инвайта снова пишутся батчем мимо громкого писателя: "
        "сбой базы здесь значит «людей пригласили, отметок нет», и повтор "
        "приглашает их снова")
    # Гаситель исключений вокруг этой записи был ЛИШНИМ ещё и потому, что
    # громкий писатель исключений не бросает: такой except — мёртвый код.
    assert not re.search(r"log_exc_swallow\(log, 'invite: ok per-target log'\)", body)
    assert not re.search(r'log_exc_swallow\(log, "invite: ok per-target log"\)', body)


def test_the_invite_outcome_marker_is_still_written():
    """Маркер исхода кормит экран ретеншена — без него приток выходит нулём."""
    body = _fn("_exec_mass_invite")
    assert "'link_sent' if" in body or '"link_sent" if' in body
    assert "joined" in body


# ── Журнал пишется только из воркера, и это проверяется, а не предполагается ──
#
# Перепись выше читает РОВНО `services/op_worker.py`. Сегодня это верно: ни
# одного `INSERT INTO operation_log` вне воркера нет. Но верно это по случаю, а
# не по построению: отметку о взятой цели мог бы начать писать `dm_engine`,
# `smart_invite` или любой другой движок, и громкости от него никто бы не
# потребовал — проверка его просто не читает. Цена именно у этой потери самая
# конкретная: пропавшая отметка «сделано» значит, что повтор ПОВТОРИТ реальное
# действие в Telegram — второй инвайт, второй пост.

_TREE_SKIP_DIRS = {"tests", "__pycache__", ".git", "node_modules", "venv",
                   ".venv", "migrations"}


def _modules_writing_the_journal() -> set[str]:
    out = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if re.search(r"INSERT\s+INTO\s+operation_log", src, re.I):
                out.add(os.path.relpath(path, ROOT))
    return out


def test_the_journal_is_written_from_one_module_only():
    """Иначе перепись громкости выше сторожит лишь часть журнала."""
    writers = _modules_writing_the_journal()
    expected = {os.path.join("services", "op_worker.py")}
    extra = writers - expected
    assert not extra, (
        f"журнал целей пишется ещё и здесь: {sorted(extra)}. Перепись "
        "громкости читает только воркер, поэтому эти записи никто не "
        "проверяет на громкость — а пропавшая отметка «сделано» значит, что "
        "повтор повторит реальное действие в Telegram. Либо пишите журнал "
        "через воркер, либо расширьте перепись на эти модули")
    assert writers == expected, (
        "воркер перестал писать журнал целей — перепись громкости выше "
        "сторожит пустоту")
