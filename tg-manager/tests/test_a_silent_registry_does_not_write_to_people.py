"""Регрессия: сбой реестра «не писать» больше не пишет тем, кто просил не писать.

РАЗРЫВ. Реестр «не писать» — не статистика, а обещание человеку: он прямо
попросил больше ему не писать. Читался реестр мягко: сбой БД давал пустой
набор, то есть «никто не отказывался», и рассылка уходила всем, включая
отказавшихся. У трёх путей, которые РЕАЛЬНО пишут людям (разовая рассылка,
кампания, масс-инвайт), этот пустой набор ещё и глотался на месте вызова, так
что ни отправитель, ни владелец ничего не узнавали.

Цена: сообщение человеку, который просил его не присылать. Для продукта это
самая верная жалоба на спам, а жалоба — прямой путь к бану аккаунта.

Теперь у чтения есть strict: экраны и пре-флайт по-прежнему обходятся пустым
набором (они ничего не отправляют), а пути отправки получают отказ, уходят на
обычный повтор операции и отправят позже — уже с фильтром. Отсрочка дешевле
письма тому, кто просил его не присылать.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from services import contact_opt_out as coo
from services.op_errors import _classify_op_error


class _Pool:
    def __init__(self, fail: int, rows: list[dict] | None = None):
        self.fail = fail
        self.rows = rows or []
        self.reads = 0

    async def execute(self, query, *args):
        return "CREATE TABLE"

    async def fetch(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась на чтении реестра")
        return self.rows


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _fast(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast)


def test_a_sending_path_refuses_to_work_without_the_registry():
    """Путь отправки обязан отказаться, а не считать, что отказов нет."""
    pool = _Pool(fail=2)
    with pytest.raises(coo.OptOutUnreadable) as e:
        _run(coo.load_opted_out(pool, 42, strict=True))
    assert pool.reads == 2, f"блип обязан лечиться повтором: чтений {pool.reads}"
    assert _classify_op_error(e.value) == "retry", (
        "отказ обязан вести к повтору операции: иначе владелец получит «не "
        "вышло» там, где достаточно было подождать")


def test_a_screen_still_survives_a_broken_registry():
    """Экран и пре-флайт ничего не отправляют — им отказ не нужен."""
    assert _run(coo.load_opted_out(_Pool(fail=2), 42)) == set()


def test_a_single_blip_is_healed():
    """Один сбой из двух — реестр прочитан, фильтр действует как обычно."""
    pool = _Pool(fail=1, rows=[{"target": "ivan"}])
    assert _run(coo.load_opted_out(pool, 42, strict=True)) == {"ivan"}
    assert pool.reads == 2


@pytest.mark.parametrize("module,func", [
    ("services/op_worker.py", "_exec_bulk_dm_adhoc"),
    ("services/op_worker.py", "_exec_mass_invite"),
    ("services/dm_engine.py", "run_campaign"),
])
def test_every_sending_path_reads_the_registry_strictly(module, func):
    """Каждый путь, который пишет людям, читает реестр строго и не глотает отказ."""
    src = open(module, encoding="utf-8").read()
    i = src.index(f"def {func}(")
    # Границу ищем по СЛЕДУЮЩЕЙ функции ВЕРХНЕГО уровня: внутри исполнителей
    # есть вложенные `async def` с отступом, и срез по ним обрезал бы тело на
    # середине — пробник краснел бы на здоровом коде.
    import re as _re
    m = _re.search(r"\n(?:async )?def [A-Za-z_]", src[i + 10:])
    seg = src[i:i + 10 + m.start()] if m else src[i:]
    assert "load_opted_out(pool, owner_id, strict=True)" in seg, (
        f"{func} читает реестр мягко: на сбое напишет тем, кто просил не писать")
    # Смотрим ровно то, что стоит ВОКРУГ вызова: у масс-инвайта чтение идёт
    # вовсе без try, у двух других — внутри try с широким глотателем, и тогда
    # отказ обязан быть пропущен наружу отдельной веткой. Проверка «есть ли
    # где-то в функции log_exc_swallow» краснела бы на здоровом коде.
    j = seg.index("load_opted_out(")
    around = seg[j:j + 600]
    if "log_exc_swallow" in around:
        assert "OptOutUnreadable" in around, (
            f"{func} глотает отказ реестра на месте вызова — отказ до повтора "
            "операции не доходит")


def test_the_refusal_is_not_swallowed_by_the_campaign_wrapper():
    """Обёртка кампании в исполнителе обязана пропустить отказ наружу.

    Она намеренно ловит только отмену задачи; всё прочее идёт в обычный повтор.
    Широкий `except Exception` на том же try вернул бы дыру целиком: кампания
    закрылась бы «выполненной», написав отказавшимся.

    Границы берём по AST: окно фиксированной длины промахнулось бы от любой
    правки рядом, и отрицательная проверка стала бы правдой сама собой.
    """
    from services import op_worker

    src = inspect.getsource(op_worker)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "_exec_dm_campaign")
    tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
             and "run_campaign(" in ast.unparse(ast.Module(body=t.body, type_ignores=[]))]
    assert tries, "вызов кампании больше не обёрнут try — проверка потеряла смысл"
    for t in tries:
        caught = {ast.unparse(h.type) if h.type else "<bare>" for h in t.handlers}
        assert "asyncio.CancelledError" in caught, (
            "обёртка перестала различать отмену задачи — кампания не встанет на паузу")
        assert not {c for c in caught if c in ("Exception", "BaseException", "<bare>")}, (
            f"обёртка кампании глотает любое исключение ({caught}) — отказ "
            "реестра не дойдёт до повтора операции")
