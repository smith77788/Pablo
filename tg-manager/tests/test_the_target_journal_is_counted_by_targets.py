"""Провалы в журнале целей считаются по ЦЕЛЯМ, а не по служебным строкам.

ЧТО ЗА СЛУЖЕБНЫЕ СТРОКИ. Исполнитель инвайта пишет в `operation_log` не только
людей, но и ШАГИ: выдачу админки (`promote`), промоут-трюк (`promote_trick`),
фолбэк ссылкой в ЛС (`link_fallback`). Это строки о работе, а не о людях.
Правда про них одна — `operation_bus.SERVICE_TARGETS` и готовый фрагмент
`REAL_TARGET_SQL`, и в комментарии рядом перечислено, чем кончалось их
незнание: прогресс «16 из 15», повтор, который не пробует настоящие цели, и
попытка пригласить пользователя с именем `promote_trick`.

ЧЕГО НЕ ХВАТАЛО. Фрагмент знают шесть читателей журнала, а СЧЁТЧИКИ ПРОВАЛОВ
(`COUNT(*) ... WHERE status='error'`) — ни один из пяти. Сегодня их не раздувает
только случай: три служебные записи инвайта писались словом `'fail'`, которого
не знает ни один читатель. То есть верное поведение держалось на слове вне
словаря — и первая же уборка «fail → error» (ровно та, с которой я начал)
зажгла бы владельцу кнопку «повторить неудавшиеся» для операции, у которой не
провалилось ни одной цели, а предохранитель Ban Weather считал бы служебную
строку сбоем типа операции.

Поэтому здесь две переписи сразу: счётчики провалов обязаны фильтровать
служебные строки, а слово статуса в журнале — быть из закрытого набора
`{ok, skip, error}`, который читатели действительно знают.
"""
from __future__ import annotations

import ast
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Слова статуса, которые читатели журнала действительно разбирают:
# 'ok' — дверь идемпотентности (completed_targets, completed_steps),
# 'skip' — закрытый вопрос (settled_targets), 'error' — провал цели.
KNOWN_JOURNAL_STATUS = {"ok", "skip", "error"}
# Слова сверх словаря — по одному, с написанной причиной. Запись здесь значит:
# это слово НЕ считается ни дверью, ни провалом, и так задумано.
EXTRA_WITH_A_REASON = {
    "flagged": "проверка на запрещёнку: цель проверена успешно, но нарушение "
               "найдено. Провалом это не считается нарочно (досье собрано, "
               "работа сделана, и предохранитель такой исход сбоем не считает), "
               "дверью — тоже: повтор скана read-only и безвреден. В покрытие "
               "журнала строка не идёт, и это ничего не ломает: покрытие только "
               "ПОВЫШАЕТ done_items до цели, а счётчик прогресса здесь и так "
               "растёт по каждой проверенной цели",
}
_FAILURE_COUNT = re.compile(r"status\s*=\s*'error'|status\s+IN\s*\('ok',\s*'error'\)")


def _sql_of_call(node: ast.Call) -> list[str]:
    """Собрать SQL из строковых частей аргументов вызова.

    Запрос в коде бывает склеен из литералов и фрагмента REAL_TARGET_SQL,
    поэтому разбор по дереву: фрагмент заменяем маркером, иначе его присутствие
    не отличить от его отсутствия.
    """
    out = []
    for a in node.args:
        txt = ""
        for sub in ast.walk(a):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                txt += " " + sub.value
            elif isinstance(sub, ast.Attribute) and sub.attr == "REAL_TARGET_SQL":
                txt += " __REAL_TARGET__ "
            elif isinstance(sub, ast.Name) and sub.id == "REAL_TARGET_SQL":
                txt += " __REAL_TARGET__ "
        if txt.strip():
            out.append(re.sub(r"\s+", " ", txt))
    return out


def _sources() -> dict[str, str]:
    out = {}
    for path in (sorted(glob.glob(os.path.join(ROOT, "services", "*.py")))
                 + sorted(glob.glob(os.path.join(ROOT, "bot", "**", "*.py"),
                                    recursive=True))):
        with open(path, encoding="utf-8") as f:
            out[os.path.relpath(path, ROOT)] = f.read()
    return out


def failure_counts_without_the_filter(src: str) -> list[int]:
    """Строки, где провалы журнала считаются без фильтра служебных строк."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        for sql in _sql_of_call(n):
            if "operation_log" not in sql or "INSERT" in sql.upper():
                continue
            if not _FAILURE_COUNT.search(sql.replace("  ", " ")):
                continue
            if "__REAL_TARGET__" in sql:
                continue
            # Запрос, уже привязанный к КОНКРЕТНОЙ цели (`target = $1`), служебную
            # строку посчитать не может: она попадёт в выборку только если
            # вызывающий сам передаст её имя. Без этой оговорки перепись давала
            # ложную находку на предохранителе Ban Weather, который считает
            # ошибки по одному аккаунту (`l.target = $1`).
            if re.search(r"target\s*=\s*\$\d", sql):
                continue
            bad.append(n.lineno)
    return sorted(set(bad))


def journal_status_words(src: str) -> set[str]:
    """Слова, которыми код ПИШЕТ статус в operation_log."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    words: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        qi = None
        for i, a in enumerate(n.args):
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and "INSERT INTO operation_log" in a.value:
                qi = i
                break
        if qi is None:
            continue
        q = re.sub(r"\s+", " ", n.args[qi].value)
        cols = re.search(r"operation_log\(([^)]*)\)", q)
        vals = re.search(r"VALUES\s*\(([^)]*)\)", q, re.I)
        if not (cols and vals):
            continue
        cl = [c.strip() for c in cols.group(1).split(",")]
        vl = [v.strip() for v in vals.group(1).split(",")]
        if "status" not in cl:
            continue
        slot = vl[cl.index("status")]
        if slot.startswith("'"):
            words.add(slot.strip("'"))
            continue
        try:
            pos = int(slot.lstrip("$")) - 1
        except ValueError:
            continue
        after = n.args[qi + 1:]
        if pos >= len(after):
            continue
        for sub in ast.walk(after[pos]):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                words.add(sub.value)
    return words


# ── Перепись первая: счётчики провалов ───────────────────────────────────────

def test_no_failure_count_over_the_journal_includes_service_rows():
    offenders = {}
    for rel, src in _sources().items():
        found = failure_counts_without_the_filter(src)
        if found:
            offenders[rel] = found
    assert not offenders, (
        "провалы журнала считаются без фильтра служебных строк: "
        f"{offenders}. Добавьте operation_bus.REAL_TARGET_SQL: иначе одна "
        "строка о выдаче админки зажжёт владельцу «повторить неудавшиеся» "
        "там, где не провалилось ни одной цели, а предохранитель Ban Weather "
        "посчитает её сбоем типа операции. Фрагмент зовите его собственным "
        "именем: перепись узнаёт его по имени, и псевдоним под `as` она не "
        "увидит — а имя при этом остаётся grep-able")


_SICK_COUNT = '''
async def f(pool, op_id):
    return await pool.fetchrow(
        "SELECT COUNT(*) FROM operation_log WHERE op_id=$1 AND status='error'", op_id)
'''
_HEALTHY_COUNT = '''
async def f(pool, op_id):
    return await pool.fetchrow(
        "SELECT COUNT(*) FROM operation_log WHERE op_id=$1 AND status='error' "
        + operation_bus.REAL_TARGET_SQL, op_id)
'''


def test_the_failure_count_census_bites():
    assert failure_counts_without_the_filter(_SICK_COUNT), (
        "перепись не видит счётчик провалов без фильтра")


def test_the_failure_count_census_accepts_the_shared_fragment():
    assert not failure_counts_without_the_filter(_HEALTHY_COUNT), (
        "фрагмент REAL_TARGET_SQL не признан фильтром — перепись потребует "
        "правок там, где всё уже верно")


# ── Перепись вторая: слово статуса ───────────────────────────────────────────

def test_the_journal_status_is_a_word_the_readers_know():
    strangers = {}
    for rel, src in _sources().items():
        words = journal_status_words(src) - KNOWN_JOURNAL_STATUS \
            - set(EXTRA_WITH_A_REASON)
        if words:
            strangers[rel] = sorted(words)
    assert not strangers, (
        f"в журнал целей пишется слово вне словаря: {strangers}. Читатели "
        f"разбирают только {sorted(KNOWN_JOURNAL_STATUS)}: 'ok' — дверь "
        "идемпотентности, 'skip' — закрытый вопрос, 'error' — провал. Чужое "
        "слово не падает ошибкой, оно просто не считается никем")


def test_the_status_census_sees_the_words_at_all():
    """Перепись, не нашедшая ни одного слова, зелена и бесполезна."""
    from_worker = journal_status_words(
        open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read())
    assert {"ok", "error"} <= from_worker, (
        f"разбор вставок сломан: нашлось {sorted(from_worker)}")


def test_the_service_targets_are_one_truth():
    """Фрагмент SQL и список имён обязаны называть ОДНО И ТО ЖЕ."""
    from services import operation_bus as ob

    in_sql = set(re.findall(r"'([a-z_]+)'", ob.REAL_TARGET_SQL))
    assert in_sql == set(ob.SERVICE_TARGETS), (
        f"список {sorted(ob.SERVICE_TARGETS)} и фрагмент {sorted(in_sql)} "
        "разошлись — читатели по фрагменту и читатели по списку начнут считать "
        "по-разному")


# ── Живая база: фрагмент действительно исключает служебную строку ────────────

@pytest.mark.asyncio
async def test_the_fragment_really_excludes_a_service_row():
    dsn = os.environ.get("INFRAGRAM_TEST_DSN")
    if not dsn:
        pytest.skip("нет INFRAGRAM_TEST_DSN")
    import asyncpg
    from services import operation_bus as ob

    conn = await asyncpg.connect(dsn)
    op_id = None
    try:
        # Журнал ссылается на очередь внешним ключом, поэтому родитель настоящий.
        op_id = int(await conn.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status) "
            "VALUES($1,$2,'done') RETURNING id", 777_000_111, "mass_invite"))
        await conn.executemany(
            "INSERT INTO operation_log(op_id, step_num, target, status) "
            "VALUES($1,0,$2,'error')",
            [(op_id, "@real_target"), (op_id, "promote_trick"), (op_id, "promote")])
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM operation_log WHERE op_id=$1 AND status='error' "
            + ob.REAL_TARGET_SQL, op_id)
        assert n == 1, (
            f"фрагмент исключил не то: провалов настоящих целей 1, посчитано {n}")
    finally:
        if op_id is not None:
            await conn.execute("DELETE FROM operation_log WHERE op_id=$1", op_id)
            await conn.execute("DELETE FROM operation_queue WHERE id=$1", op_id)
        await conn.close()


def test_every_extra_word_carries_a_real_reason():
    import re as _re

    for word, reason in sorted(EXTRA_WITH_A_REASON.items()):
        assert reason and _re.search(r"[а-яА-Я]", reason), (
            f"{word}: причина обязана быть по-русски — её читает владелец кода")
        assert len(reason) > 60, f"{word}: причина слишком коротка: {reason!r}"


def test_no_extra_word_is_dead():
    """Слово убрали из кода — запись перестаёт что-либо значить."""
    written: set[str] = set()
    for src in _sources().values():
        written |= journal_status_words(src)
    gone = sorted(set(EXTRA_WITH_A_REASON) - written)
    assert not gone, (
        f"эти слова в журнал больше никто не пишет, уберите их из "
        f"EXTRA_WITH_A_REASON: {gone}")
