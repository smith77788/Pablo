"""Кнопка «Повторить» доделывает остаток, а не переделывает всё заново.

ЧТО БЫЛО. Три ключа идемпотентности (`completed_targets`, `settled_targets`,
`completed_steps`) читают operation_log ПО op_id. Пока повтор — это возврат той
же строки очереди в pending, ключ тот же и всё сходится. Но кнопка «Повторить»
ставит НОВУЮ операцию с новым id: журнал у неё пустой, и исполнитель честно
проходит весь список целей сначала.

Для владельца это выглядит так: рассылка встала на 203 адресатах из 380, он
жмёт «Повторить» — и 203 человека получают ВТОРОЕ одинаковое сообщение. Самая
заметная спам-сигнатура, ровно та, от которой журнал и защищает. Для постинга —
второй пост в каналах, которые его уже получили; для создания каналов — второй
комплект каналов, а это необратимо.

Повторить при этом можно только НЕДОВЕДЁННУЮ работу (operation_retry.can_retry
отказывает полностью выполненной), поэтому пропуск сделанного — именно то, чего
владелец ждёт от кнопки.

ЧТО ТЕПЕРЬ. Связь повтора с оригиналом в данных уже была — `retry_of_op` в
params, который ставил operation_bus.submit_retry_failed и не читал никто.
Теперь по ней и идём: журнал собирается по всей цепочке повторов, а кнопка
мини-аппа эту ссылку проставляет.
"""
from __future__ import annotations

import ast
import inspect

import pytest


class _ChainPool:
    """Пул, который знает родителя каждой операции и записывает запрос журнала."""

    def __init__(self, parents: dict[int, int | None]):
        self.parents = parents
        self.journal_args: list = []

    async def fetchrow(self, query, *args):
        if "retry_of_op" in query:
            src = self.parents.get(int(args[0]))
            return {"src": str(src) if src is not None else None}
        return None

    async def fetch(self, query, *args):
        if "operation_log" in query:
            self.journal_args.append(args[0])
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_operation_without_a_parent_reads_only_its_own_journal():
    from services import op_worker

    pool = _ChainPool({10: None})
    assert await op_worker.journal_op_ids(pool, 10) == [10]


@pytest.mark.asyncio
async def test_retry_reads_the_journal_of_the_operation_it_repeats():
    from services import op_worker

    pool = _ChainPool({11: 10, 10: None})
    chain = await op_worker.journal_op_ids(pool, 11)

    assert chain == [11, 10], (
        "повтор не видит журнал исходной операции — 203 адресата из 380 получат "
        "второе одинаковое сообщение"
    )


@pytest.mark.asyncio
async def test_chain_of_repeats_is_followed_to_the_start():
    from services import op_worker

    pool = _ChainPool({13: 12, 12: 11, 11: 10, 10: None})
    assert await op_worker.journal_op_ids(pool, 13) == [13, 12, 11, 10]


@pytest.mark.asyncio
async def test_broken_data_cannot_loop_forever():
    from services import op_worker

    pool = _ChainPool({21: 22, 22: 21})
    chain = await op_worker.journal_op_ids(pool, 21)

    assert chain == [21, 22], "цикл в данных увёл обход в бесконечность"
    assert len(chain) <= op_worker._RETRY_CHAIN_MAX + 1


@pytest.mark.asyncio
async def test_unreadable_chain_falls_back_to_own_journal():
    """Не прочитали цепочку — работаем как раньше, а не падаем."""
    from services import op_worker

    class _Broken:
        async def fetchrow(self, query, *args):
            raise RuntimeError("база недоступна")

        async def fetch(self, query, *args):
            return []

    assert await op_worker.journal_op_ids(_Broken(), 7) == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["completed_targets", "settled_targets", "completed_steps"])
async def test_every_idempotency_key_spans_the_retry_chain(helper):
    from services import op_worker

    pool = _ChainPool({31: 30, 30: None})
    await getattr(op_worker, helper)(pool, 31)

    assert pool.journal_args, f"{helper} не обратился к журналу"
    assert set(pool.journal_args[0]) == {31, 30}, (
        f"{helper} читает журнал только новой операции — повтор переделает "
        f"работу, уже сделанную исходной"
    )


def _handler_src(module, name: str) -> str:
    """Тело функции по границам AST, а не окном фиксированной длины.

    Окно живёт до следующей правки: подросла функция — срез перестал совпадать с
    тем, что имелось в виду, и ОТРИЦАТЕЛЬНОЕ утверждение о нём зеленеет навсегда
    (в чужом куске искомого нет). Это запрещено — tests/test_no_silently_disabled_guards.py.
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {module.__name__}")


def test_miniapp_retry_links_the_new_operation_to_the_old_one():
    from services import mini_app_api

    body = _handler_src(mini_app_api, "retry_operation")

    # Ссылку ставит общий сборщик params повтора
    # (operation_bus.params_for_retry) — он же снимает ключи расписания, чтобы
    # повтор круга автопостинга не заводил вторую цепочку публикаций. Проверка
    # стала строже: раньше хватало literal "retry_of_op" в любом виде.
    assert body.count("params_for_retry(") == 2, (
        "кнопка «Повторить» ставит операцию без ссылки на исходную — журнал у "
        "неё пустой, и вся уже сделанная работа будет сделана второй раз "
        "(оба пути: точечный повтор публикации и повтор целиком)"
    )
    # Комментарии выбрасываем: они про retry_of_op ГОВОРЯТ, а проверка про то,
    # что params им не собирают на месте.
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert "retry_of_op" not in code, (
        "params повтора собираются на месте, мимо operation_bus.params_for_retry: "
        "так уже терялись и ссылка на журнал, и защита от клона расписания"
    )
