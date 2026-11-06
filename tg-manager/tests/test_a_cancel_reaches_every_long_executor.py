"""Отмена доходит до каждого исполнителя, который ходит по целям.

ЗАЧЕМ ЗАКРЫТАЯ ПЕРЕПИСЬ. «Отменить» — аварийный тормоз на самой дорогой
стороне продукта: владелец нажимает его, когда флот уже рассылает, приглашает
или жалуется. Своей задачи у исполнителя нет, поэтому отмена доходит до него
ОПРОСОМ, и опрос обязан стоять ВНУТРИ прохода по целям. Проверка перед циклом
бесполезна по построению: в тот момент владелец ещё ничего не нажал.

Перепись закрытая: берутся ВСЕ `_exec_*` с циклом, внутри которого есть
ожидание. Каждый обязан либо спрашивать отмену внутри цикла, либо стоять в
`DELEGATED` с написанной причиной — то есть передавать сам проход движку,
который спрашивает отмену у той же общей двери. Третьего варианта нет.

ПОЧЕМУ ТАК, А НЕ СПИСКОМ ИМЁН. Исполнителей шестьдесят с лишним, и новый
массовый исполнитель в рукописный список не попадёт, а снаружи это выглядит
как «нажал отмену, а флот работает ещё час».
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Исполнители живут не только в op_worker: передача подарков, например, вынесена
# в services/gift_operation.py. Перепись по одному файлу была бы закрытой только
# на вид (так она и пропустила _exec_gift_transfer в первом заходе).
LAYER = ("services",)

# Как спрашивают отмену: общая дверь `op_status.stop_requested` и её обёртки.
CANCEL_TOKENS = ("_is_cancelled", "stop_requested", "_op_cancelled",
                 "is_cancelled", "_cancelled(")

# Исполнители, у которых сам проход по целям живёт в движке. Для каждого —
# модуль движка: он обязан спрашивать отмену, и это проверяется ниже.
DELEGATED: dict[str, tuple[str, str]] = {
    "_exec_run_broadcast": (
        "services/broadcaster.py",
        "цикл отправки живёт в broadcaster: он читает решение владельца "
        "общей дверью op_status.stop_requested на каждом батче",
    ),
    "_exec_strike": (
        "services/strike_engine.py",
        "волны ведёт staggered_strike: отмена спрашивается на чекпоинтах "
        "между волнами и целями, иначе страйк доигрывал бы до конца",
    ),
    "_exec_find_contact": (
        "services/contact_finder.py",
        "перебор username ведёт contact_finder.hunt: исполнитель передаёт "
        "ему колбэк is_cancelled, и опрос идёт внутри перебора",
    ),
}


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _looping_executors(src: str) -> dict[str, ast.AST]:
    """`_exec_*`, у которых есть цикл с ожиданием внутри."""
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name.startswith("_exec_")):
            continue
        loops = [x for x in ast.walk(node)
                 if isinstance(x, (ast.For, ast.AsyncFor))
                 and any(isinstance(y, ast.Await) for y in ast.walk(x))]
        if loops:
            out[node.name] = node
    return out


def _layer_executors() -> dict[str, tuple[str, str, ast.AST]]:
    """{имя: (файл, исходник файла, узел)} по всему слою исполнителей."""
    out: dict[str, tuple[str, str, ast.AST]] = {}
    for sub in LAYER:
        for path in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            src = path.read_text(encoding="utf-8")
            if "_exec_" not in src:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for name, node in _looping_executors(src).items():
                out[name] = (rel, src, node)
    return out


def _asks_inside_a_loop(src: str, node) -> bool:
    lines = src.splitlines()
    for loop in ast.walk(node):
        if not (isinstance(loop, (ast.For, ast.AsyncFor))
                and any(isinstance(y, ast.Await) for y in ast.walk(loop))):
            continue
        seg = "\n".join(lines[loop.lineno - 1:(loop.end_lineno or loop.lineno)])
        if any(t in seg for t in CANCEL_TOKENS):
            return True
    return False


def test_every_looping_executor_asks_about_cancellation_inside_the_loop():
    execs = _layer_executors()
    bad = [f"{rel}:{name}" for name, (rel, src, node) in execs.items()
           if name not in DELEGATED and not _asks_inside_a_loop(src, node)]
    assert not bad, (
        "исполнитель ходит по целям и не спрашивает отмену внутри прохода — "
        "владелец нажмёт «Отменить», а флот будет работать до конца списка: "
        + ", ".join(sorted(bad))
    )


def test_the_census_is_not_empty():
    """Пустая перепись сделала бы проверку выше вечно зелёной."""
    execs = _layer_executors()
    assert len(execs) >= 40, f"найдено исполнителей с проходом: {len(execs)}"
    for must in ("_exec_mass_invite", "_exec_gift_transfer"):
        assert must in execs, f"{must} не попал в перепись — разбор сломан"


def test_the_delegated_engines_really_ask():
    """Запись в списке исключений обязана оставаться правдой."""
    for name, (engine_rel, reason) in DELEGATED.items():
        src = _source(engine_rel)
        assert any(t in src for t in CANCEL_TOKENS), (
            f"{name} отдан движку {engine_rel}, а тот отмену не спрашивает — "
            "значит отмена не доходит вовсе")
        assert len(reason.strip()) > 20, f"{name}: причина не написана"


def test_no_stale_names_in_the_delegated_list():
    """Исчезнувший исполнитель в списке исключений прячет нового такого же."""
    execs = _layer_executors()
    stale = [n for n in DELEGATED if n not in execs]
    assert not stale, f"в списке исключений имена, которых больше нет: {stale}"


def test_a_delegated_entry_is_not_hiding_an_honest_executor():
    """Если исполнитель и сам спрашивает отмену — он не исключение, а норма."""
    execs = _layer_executors()
    needless = [n for n in DELEGATED
                if n in execs and _asks_inside_a_loop(execs[n][1], execs[n][2])]
    assert not needless, (
        "эти исполнители спрашивают отмену сами — уберите их из списка "
        f"исключений, иначе он перестанет значить что-либо: {needless}")


def test_the_detector_sees_a_check_only_before_the_loop():
    """Самопроверка: мерка обязана отличать «до цикла» от «внутри цикла»."""
    sick = (
        "async def _exec_sick(pool, bot, op_id, owner_id, params):\n"
        "    if await _is_cancelled(pool, op_id):\n"
        "        return {'status': 'cancelled'}\n"
        "    for t in params['targets']:\n"
        "        await send(t)\n"
    )
    healthy = (
        "async def _exec_ok(pool, bot, op_id, owner_id, params):\n"
        "    for t in params['targets']:\n"
        "        if await _is_cancelled(pool, op_id):\n"
        "            break\n"
        "        await send(t)\n"
    )
    for src, expect in ((sick, False), (healthy, True)):
        execs = _looping_executors(src)
        assert execs, "пример не попал в перепись"
        node = next(iter(execs.values()))
        assert _asks_inside_a_loop(src, node) is expect, src


def test_the_pause_intent_is_separate_from_cancellation():
    """Пауза идущей операции — не отмена, и это сознательно.

    `pause_requested` запоминает намерение владельца и применяется, когда
    проход закончится сам; прервать проход без чекпоинта означало бы
    пере-прогон и дубли реальных действий. Проверка держит сам факт, что
    намерение хранится отдельно от отмены: если `pause_requested` исчезнет,
    пауза на идущей операции снова начнёт молча отпускаться.
    """
    src = _source("services/op_worker.py")
    assert "pause_requested" in src
    assert re.search(r"status='paused'[^\n]*\n[^\n]*pending'\s*AND\s*pause_requested",
                     src) or "AND pause_requested" in src, (
        "намерение паузы больше не применяется при входе в работу")
