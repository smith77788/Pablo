"""Процесс, потерявший аренду исполнителя, обязан отпустить работу.

РАЗРЫВ. Аренда исполнителя (`executor_lease`) решает, кто разбирает очередь:
держатель работает, остальные ждут. Гейт в `run` закрывает НОВУЮ работу — и на
этом всё. Операции, уже запущенные в этом процессе, живут своими asyncio-задачами
и про аренду не знают.

Что из этого следует, если аренду забрал другой процесс (наш круг завис дольше
TTL, БД была недоступна, роль переехала):

  * новый держатель первым делом зовёт `_reset_stale_running` — для него наши
    живые операции выглядят жертвами падения. Он возвращает их в очередь и
    запускает ЗАНОВО, пока наши задачи ещё идут: те же приглашения тем же
    людям, второй пост в канал, второе сообщение человеку;
  * продление аренды АККАУНТОВ (`renew_leases`) стоит в цикле ПОСЛЕ гейта,
    то есть процесс, потерявший аренду исполнителя, перестаёт продлевать и
    аренды сессий. Через TTL новый держатель законно забирает аккаунты, которыми
    наши задачи прямо сейчас пользуются — это AUTH_KEY_DUPLICATED, ровно та
    поломка, от которой аренда и защищает, и сгоревшие аккаунты.

То есть аренда защищала только половину: она останавливала того, кто ещё НЕ
начал, и не останавливала того, кто уже идёт.

ПОЧЕМУ СТОП, А НЕ ВОЗВРАТ В ОЧЕРЕДЬ. Очередь с этой секунды чужая: наш UPDATE
`WHERE status='running'` попал бы по строке, которую новый держатель уже
перезапустил, и сбросил бы ЕГО живую операцию. Поэтому делаем только то, что
наше: гасим свои задачи и отпускаем свои аренды аккаунтов. Вернуть операции в
очередь — дело держателя, у него для этого есть оба сторожа.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


def _read() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _fn_src(name: str) -> str:
    src = _read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"в op_worker нет функции {name}")


@pytest.fixture
def ow():
    from services import op_worker
    yield op_worker


@pytest.fixture(autouse=True)
def _clean_state(ow):
    yield
    ow._active_op_ids.clear()
    ow._active_op_tasks.clear()
    ow._accounts_in_use.clear()


class _Pool:
    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.executed: list = []

    async def execute(self, q, *a):
        if self.raises:
            raise RuntimeError("БД недоступна")
        self.executed.append((q, a))
        return "UPDATE 0"

    async def fetch(self, q, *a):
        if self.raises:
            raise RuntimeError("БД недоступна")
        return []

    async def fetchrow(self, q, *a):
        if self.raises:
            raise RuntimeError("БД недоступна")
        return None


def _stand_down(ow, pool, **kw):
    async def _go():
        return await ow._stand_down_after_lease_loss(pool, **kw)
    return asyncio.run(_go())


# ── Главное: работа останавливается ──────────────────────────────────────────

def test_in_flight_operations_are_stopped(ow):
    """Иначе новый держатель запустит их второй раз поверх идущих."""
    state = {"cancelled": False}

    async def _go():
        async def _forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(4242)
        res = await ow._stand_down_after_lease_loss(_Pool(), grace_s=1.0)
        assert task.done(), "задача операции осталась жить без аренды исполнителя"
        return res

    res = asyncio.run(_go())
    assert state["cancelled"], "задача не была отменена"
    assert res["stopped"] == 1, f"не отчитались об остановке: {res}"


def test_our_account_leases_are_released(ow, monkeypatch):
    """Не отпустить их значит держать флот до TTL, ничего им не делая."""
    released: list = []

    async def _fake_release(ids):
        released.append(sorted(int(i) for i in ids))

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    ow._accounts_in_use.update({11, 12})
    ow._active_op_ids.add(7)

    res = _stand_down(ow, _Pool())
    assert released == [[11, 12]], f"аренды аккаунтов не сняты: {released}"
    assert not ow._accounts_in_use, "память о занятых аккаунтах не очищена"
    assert res["released"] == 2


def test_accounts_are_released_only_after_the_tasks_are_stopped(ow, monkeypatch):
    """Обратный порядок — это AUTH_KEY_DUPLICATED и сгоревшие аккаунты.

    Пока задача не свернулась, она ещё работает сессией. Отпустить аренду
    раньше значит отдать эту сессию новому держателю в живом виде.
    """
    order: list[str] = []

    async def _fake_release(ids):
        order.append("released")

    monkeypatch.setattr(ow, "_db_release", _fake_release)

    async def _go():
        async def _forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("task-stopped")
                raise

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(5)
        ow._accounts_in_use.add(99)
        await ow._stand_down_after_lease_loss(_Pool(), grace_s=1.0)

    asyncio.run(_go())
    assert order == ["task-stopped", "released"], f"неверный порядок: {order}"


# ── Чего делать нельзя ───────────────────────────────────────────────────────

def test_the_queue_is_not_touched():
    """Очередь с этой секунды чужая: наш UPDATE попал бы по чужой операции."""
    body = _fn_src("_stand_down_after_lease_loss")
    assert "operation_queue" not in body, (
        "сдача аренды пишет в очередь — этот UPDATE попадёт по операции, "
        "которую новый держатель уже перезапустил")


def test_the_process_is_not_marked_as_shutting_down(ow):
    """Аренду можно получить назад — процесс обязан уметь продолжить."""
    before = ow._shutting_down
    _stand_down(ow, _Pool())
    assert ow._shutting_down == before, (
        "сдача аренды объявила процесс останавливающимся — вернув аренду, "
        "он уже не возьмёт работу")


def test_it_never_raises_even_if_the_database_is_gone(ow, monkeypatch):
    """Её зовут из цикла воркера: исключение здесь остановит весь исполнитель."""
    async def _boom(ids):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(ow, "_db_release", _boom)
    ow._accounts_in_use.add(3)
    ow._active_op_ids.add(3)
    _stand_down(ow, _Pool(raises=True))


def test_with_nothing_in_flight_it_does_nothing(ow, monkeypatch):
    """Гейт срабатывает каждые 10 секунд — тишина обязана быть бесплатной."""
    calls: list = []

    async def _fake_release(ids):
        calls.append(ids)

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    res = _stand_down(ow, _Pool())
    assert calls == [], "пустая сдача аренды всё равно трогала аккаунты"
    assert res == {"stopped": 0, "released": 0}


# ── Связка с циклом и самопроверка пробника ──────────────────────────────────

def test_standing_down_is_wired_to_the_lease_check():
    """Функция, которую не зовут из ветки отказа, не чинит ничего."""
    src = _read()
    tree = ast.parse(src)
    run = next(n for n in tree.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")

    def _calls(node) -> set[str]:
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                out.add(f.attr if isinstance(f, ast.Attribute)
                        else getattr(f, "id", ""))
        return out

    denied = [n for n in ast.walk(run)
              if isinstance(n, ast.If) and "acquire_executor_lease" in _calls(n.test)]
    assert denied, "в цикле воркера нет ветки отказа аренды исполнителя"
    assert any("_stand_down_after_lease_loss" in _calls(branch)
               for node in denied for branch in node.body), (
        "потеряв аренду, процесс только пропускает круг — его операции "
        "продолжают идти поверх перезапущенных новым держателем")


def test_the_probe_would_notice_a_stand_down_that_stops_nothing(ow):
    """Проверка измерителя: тест выше обязан падать на пустой реализации."""
    async def _go():
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(1)
        # Намеренно НЕ зовём сдачу аренды — задача обязана остаться живой,
        # иначе тест «операции остановлены» прошёл бы сам собой.
        alive = not task.done()
        task.cancel()
        return alive

    assert asyncio.run(_go()), "задача умирает без сдачи аренды — пробник слеп"
