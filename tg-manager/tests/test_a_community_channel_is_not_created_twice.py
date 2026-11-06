"""Повтор операции «каналы ноды» не создаёт второй комплект топиков.

`community_add_channel` создаёт в супергруппе ноды до двадцати форум-топиков в
цикле. Отметок возобновления у этого исполнителя не было ни одной: ни
`completed_steps`, ни `completed_targets`, ни даже `done_items`. Любое
воскрешение операции (сброс зависшей, таймаут прогона, перезапуск воркера)
начинало цикл с первого имени заново.

Телеграм при этом НЕ склеивает одноимённые топики: `create_forum_topic`
каждый раз выдаёт новый `message_thread_id`, поэтому и `ON CONFLICT
(node_id, tg_topic_id)` в `nodes_engine.create_community_channel` не
срабатывает. То есть повтор давал владельцу ВТОРОЙ комплект топиков с теми же
названиями, и убрать их можно только руками.

Отдельно важно, что владельцу про такой повтор обещано обратное: сообщения
про воскрешение и про снятие с потолка говорят «уже отработанные цели повтор
пропустит». Для этого типа операции обещание было ложным.

Ключ идемпотентности — номер шага (`completed_steps`), а не имя: имена в
списке могут повторяться, а позиция в пакете устойчива, цикл всегда идёт по
`names` в одном порядке.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from services import op_worker as ow

ROOT = pathlib.Path(__file__).resolve().parents[1]
OP = 9100
OWNER = 4242
NODE = 77


class _Pool:
    async def execute(self, query, *args):
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetchval(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []


def _run(monkeypatch, names, done_steps, fail_read=False, cancel=None,
         mark_fails=False):
    """Один прогон исполнителя. Возвращает (созданные имена, отметки, итог)."""
    created: list[str] = []
    marks: list[tuple] = []

    async def _create(pool, bot, owner_id, node_id, name):
        created.append(str(name))
        return {"id": len(created), "name": str(name)}

    async def _steps(pool, op_id):
        if fail_read:
            raise RuntimeError("журнал недоступен")
        return set(done_steps)

    async def _jd(pool, op_id, step_num, target, message=None):
        marks.append((op_id, step_num, target, message))
        if callable(mark_fails):
            return not mark_fails(step_num)
        return not mark_fails

    async def _no(pool, op_id):
        return False if cancel is None else await cancel(pool, op_id)

    async def _noop(*a, **kw):
        return None

    from services import nodes_engine
    monkeypatch.setattr(nodes_engine, "create_community_channel", _create)
    monkeypatch.setattr(ow, "completed_steps", _steps)
    monkeypatch.setattr(ow, "_journal_done", _jd)
    monkeypatch.setattr(ow, "_is_cancelled", _no)
    monkeypatch.setattr(ow, "_mark_progress", _noop)
    monkeypatch.setattr(ow, "_declare_total_items", _noop)
    monkeypatch.setattr(ow.random, "uniform", lambda a, b: 0)

    res = asyncio.run(ow._exec_community_add_channel(
        _Pool(), object(), OP, OWNER, {"node_id": NODE, "names": list(names)}))
    return created, marks, res


def test_a_repeat_skips_the_topics_already_created(monkeypatch):
    """Первые два шага отработаны — повтор создаёт только третий топик."""
    created, _, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps={1, 2})
    assert created == ["Барахолка"], created
    assert res["status"] == "done", res
    assert res["created"] == 3, (
        "в итоге должны быть все три канала ноды, а не только созданный сейчас")


def test_every_created_topic_leaves_a_mark(monkeypatch):
    """Без отметки дверь на следующем прогоне закрыта быть не может."""
    created, marks, _ = _run(monkeypatch, ["Новости", "Чат"], done_steps=set())
    assert created == ["Новости", "Чат"]
    assert len(marks) == 2, f"отметок об успехе {len(marks)}, а каналов создано 2"
    assert [m[1] for m in marks] == [1, 2], f"номера шагов: {[m[1] for m in marks]}"
    assert [m[2] for m in marks] == ["Новости", "Чат"], [m[2] for m in marks]


def test_an_unreadable_journal_does_not_create_a_second_set(monkeypatch):
    """Моргнул журнал — отказ, а не второй комплект топиков.

    Создание необратимо, поэтому неизвестность трактуется в пользу «уже
    сделано»: операция уйдёт на повтор, когда журнал ответит.
    """
    with pytest.raises(RuntimeError):
        _run(monkeypatch, ["Новости"], done_steps=set(), fail_read=True)


def test_a_cancelled_run_reports_what_it_managed(monkeypatch):
    """Отмена посреди пакета не теряет уже созданное."""
    async def _cancel_after_first(pool, op_id):
        _cancel_after_first.n += 1
        return _cancel_after_first.n > 1
    _cancel_after_first.n = 0

    created, marks, res = _run(
        monkeypatch, ["Новости", "Чат"], done_steps=set(),
        cancel=_cancel_after_first)
    assert created == ["Новости"], created
    assert res["created"] == 1, res


def test_the_handler_still_reads_the_resume_door():
    """Ратчет: исполнитель каналов ноды спрашивает журнал перед созданием.

    Проверка по дереву, а не по подстроке: про `completed_steps` в этом файле
    рассказывает и докстринг, и он совпал бы с поиском по тексту.
    """
    import ast

    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_exec_community_add_channel"), None)
    assert fn is not None, "исполнитель каналов ноды пропал"
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "completed_steps" in called, (
        "повтор снова создаст второй комплект топиков ноды")
    assert "_journal_done" in called, (
        "созданный топик снова не оставляет отметки — дверь нечем закрывать")


def test_two_lost_marks_in_one_pass_stop_the_batch(monkeypatch):
    """Вторая потеря в проходе — дверь не работает, пачку останавливаем.

    Топик уже создан, отменить его нечем, и повтор создаст его второй раз:
    это цена одного элемента, и из-за блипа базы рвать пачку из двадцати имён
    дороже, чем её доделать. Но ДВЕ потери подряд — это уже не блип, и каждый
    следующий топик добавит к повтору ещё одну копию. Порог один на все
    создающие операции — `_MAX_UNMARKED_PER_PASS`.
    """
    created, marks, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps=set(),
        mark_fails=True)
    assert created == ["Новости", "Чат"], (
        f"пачка не остановилась на второй потере: {created}")
    assert len(marks) == 2, marks
    assert res["unmarked"] == 2, res
    assert res["created"] == 2, res
    assert "Без отметки" in res["summary"], res["summary"]
    assert "второй раз" in res["summary"], (
        "владельцу не сказано, чем грозит потерянная отметка")


def test_one_lost_mark_does_not_stop_the_batch(monkeypatch):
    """Одна потеря — работаем дальше, но говорим о ней владельцу."""
    fails = {"n": 0}

    def _once(_):
        fails["n"] += 1
        return fails["n"] == 1

    created, marks, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps=set(),
        mark_fails=_once)
    assert created == ["Новости", "Чат", "Барахолка"], (
        f"блип базы оставил пачку недоделанной: {created}")
    assert res["unmarked"] == 1, res
    assert res["created"] == 3, res
    assert "Без отметки 1" in res["summary"], res["summary"]


def test_a_working_journal_does_not_stop_the_batch(monkeypatch):
    """Обратная сторона того же: исправный журнал пачку не прерывает."""
    created, _, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps=set())
    assert created == ["Новости", "Чат", "Барахолка"], created
    assert res["unmarked"] == 0, res
    assert "Без отметки" not in res["summary"], res["summary"]
