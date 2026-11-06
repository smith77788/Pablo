"""Создающий исполнитель обязан ЗАМЕТИТЬ, что отметка о цели не записалась.

`_journal_done` пишет в журнал строку «цель сделана» — ту самую, по которой
`completed_steps` на следующем проходе решает, что пропустить. При сбое базы он
делает одну повторную попытку, потом пишет log.error, считает метрику и
возвращает False. Операцию он сознательно не роняет: цель уже создана, падать
поздно.

Но возвращаемое значение не читал НИКТО: все 23 вызова выбрасывали его. Для
отчётных операций это терпимо — потеря видна в логе и на графике. Для
СОЗДАЮЩИХ (фабрика ботов, фабрика каналов, каналы ноды) потерянная отметка
значит, что повтор создаст объект второй раз: лишний бот жжёт жёсткий предел
BotFather в 20 штук на аккаунт, лишний канал — суточный лимит и темп создания,
на который смотрит Telegram. Удалять и то и другое владельцу придётся руками.

Правило одно на всех, порог в `_MAX_UNMARKED_PER_PASS`: одну потерю проход
переживает (типовая причина мгновенная — занятый пул, разрыв на деплое), но
говорит о ней владельцу в итоге; на второй потере в том же проходе пачка
останавливается — дверь не работает, и каждый следующий объект добавит к
повтору ещё одну копию.

Здесь проверяется связка по дереву: что результат вызова действительно
читается, что порог взят из общей константы и что владельцу про потерю
сказано. Поведение самого правила проверяется на исполнителе каналов ноды
(`test_a_community_channel_is_not_created_twice.py`) — у него для этого есть
дешёвый стенд.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Исполнители, у которых дверь возобновления — журнал шагов, а действие
# необратимо. Операции global presence сюда НЕ входят: они возобновляются по
# своей таблице `global_presence_targets`, и строка в operation_log для них —
# отчёт, а не дверь.
CREATING = (
    "_exec_bot_factory",
    "_exec_bot_factory_multi",
    "_exec_bulk_create_channels",
    "_exec_bulk_create_channels_multi",
    "_exec_community_add_channel",
)


@pytest.fixture(scope="module")
def ow():
    """Дерево и текст op_worker: проверки идут по разбору, а не по подстроке."""
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        src = f.read()
    return ast.parse(src), src.splitlines()


def _fn(ow, name):
    tree, _ = ow
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == name), None)
    assert fn is not None, f"не стало исполнителя {name}"
    return fn


def _text(ow, name) -> str:
    _, lines = ow
    fn = _fn(ow, name)
    return "\n".join(lines[fn.lineno - 1:fn.end_lineno])


def _is_journal_done(call) -> bool:
    f = getattr(call, "func", None)
    return isinstance(call, ast.Call) and (
        getattr(f, "id", None) or getattr(f, "attr", None)) == "_journal_done"


def _bare_marks(fn) -> list[int]:
    """Вызовы, чей результат выброшен: сам вызов — целая инструкция."""
    return [n.value.value.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
            and _is_journal_done(n.value.value)]


@pytest.mark.parametrize("name", CREATING)
def test_the_mark_result_is_read(ow, name):
    """Выброшенный результат — это молча потерянная дверь идемпотентности."""
    fn = _fn(ow, name)
    assert [c for c in ast.walk(fn) if _is_journal_done(c)], (
        f"{name}: ни одной отметки о сделанной цели — повтор создаст всё заново")
    bare = _bare_marks(fn)
    assert not bare, (
        f"{name}: результат записи отметки выброшен, строки {bare}. "
        f"Потерянная отметка значит второй созданный объект на повторе")


@pytest.mark.parametrize("name", CREATING)
def test_the_pass_stops_on_the_shared_threshold(ow, name):
    """Порог общий: иначе правило разъедется между исполнителями."""
    fn = _fn(ow, name)
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "_MAX_UNMARKED_PER_PASS" in names, (
        f"{name}: порог потерь свой, а не общий")
    assert any(isinstance(n, ast.Break) for n in ast.walk(fn)), (
        f"{name}: пачку нечем остановить")


@pytest.mark.parametrize("name", CREATING)
def test_the_owner_is_told_about_a_lost_mark(ow, name):
    """Потеря, видная только в логе, для владельца не существует."""
    assert "Без отметки" in _text(ow, name), (
        f"{name}: про потерянную отметку сказано только в лог, а владелец "
        f"увидит «создано N» и не узнает про возможную копию")


def test_the_threshold_is_not_zero_or_one(ow):
    """Порог в ноль или единицу означал бы остановку на первом же блипе."""
    tree, _ = ow
    val = next((n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(getattr(x, "id", "") == "_MAX_UNMARKED_PER_PASS"
                        for x in n.targets)
                and isinstance(n.value, ast.Constant)), None)
    assert isinstance(val, int) and val >= 2, (
        f"_MAX_UNMARKED_PER_PASS = {val}: одна потерянная отметка — это один "
        f"возможный дубль, а не повод бросить остальную пачку")


def test_the_measurer_tells_a_read_result_from_a_discarded_one():
    """Самопроверка детектора на двух заведомо известных примерах."""
    bad = ast.parse("async def f():\n    await _journal_done(p, 1, 2, 't')\n")
    good = ast.parse(
        "async def f():\n"
        "    if not await _journal_done(p, 1, 2, 't'):\n        pass\n")
    assert _bare_marks(bad.body[0]), "детектор не видит выброшенный результат"
    assert not _bare_marks(good.body[0]), (
        "детектор считает прочитанный результат выброшенным")


def test_a_reporting_executor_is_not_held_to_this_rule(ow):
    """Границу правила держим осознанно.

    `_exec_global_presence_channel` тоже создаёт каналы, но возобновляется по
    своей таблице `global_presence_targets`: строка в operation_log для него —
    отчёт. Если он однажды перейдёт на журнал шагов, его надо будет внести в
    CREATING, и этот тест про это напомнит.
    """
    body = _text(ow, "_exec_global_presence_channel")
    assert "global_presence_targets" in body, (
        "у global presence пропала своя дверь возобновления — правило выше "
        "теперь должно распространяться и на него")
    assert "completed_steps" not in body, (
        "global presence перешёл на журнал шагов — внесите его в CREATING")
