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

    async def _jw(pool, query, *args, **kw):
        marks.append((query, args))
        return "ERROR" if mark_fails else "INSERT 1"

    async def _no(pool, op_id):
        return False if cancel is None else await cancel(pool, op_id)

    async def _noop(*a, **kw):
        return None

    from services import nodes_engine
    monkeypatch.setattr(nodes_engine, "create_community_channel", _create)
    monkeypatch.setattr(ow, "completed_steps", _steps)
    monkeypatch.setattr(ow, "_journal_write", _jw)
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
    ok = [a for q, a in marks if "'ok'" in q]
    assert len(ok) == 2, f"отметок об успехе {len(ok)}, а каналов создано 2"
    assert [a[1] for a in ok] == [1, 2], f"номера шагов: {[a[1] for a in ok]}"
    assert [a[2] for a in ok] == ["Новости", "Чат"], [a[2] for a in ok]


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
    assert "_journal_write" in called, (
        "созданный топик снова не оставляет отметки — дверь нечем закрывать")


def test_a_lost_mark_stops_the_batch(monkeypatch):
    """Отметка не записалась — пачку останавливаем на первом же топике.

    Топик уже создан, отменить его нечем, и повтор создаст его второй раз:
    это цена одного элемента. Но если журнал отказывает, он откажет и на
    следующих, поэтому продолжать пачку значит множить дубли. Тот же счёт
    появился в `_exec_deploy_network` после прогона, который закрывался
    «готово», а следующий создавал второй такой же канал.
    """
    created, marks, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps=set(),
        mark_fails=True)
    assert created == ["Новости"], (
        f"пачка пошла дальше с неработающим журналом: {created}")
    assert len(marks) == 1, marks
    assert res["unmarked"] == 1, res
    assert res["created"] == 1, res
    assert "без отметки" in res["summary"], res["summary"]
    assert "запустите заново" in res["summary"], (
        "владельцу не сказано, что остальные имена остались несделанными")


def test_a_working_journal_does_not_stop_the_batch(monkeypatch):
    """Обратная сторона того же: исправный журнал пачку не прерывает."""
    created, _, res = _run(
        monkeypatch, ["Новости", "Чат", "Барахолка"], done_steps=set())
    assert created == ["Новости", "Чат", "Барахолка"], created
    assert res["unmarked"] == 0, res
    assert "без отметки" not in res["summary"], res["summary"]
