"""Отмена операции сильнее повтора, а плановый рестарт не съедает живучесть.

ДВА КЛАССА, КОТОРЫЕ ЭТОТ ФАЙЛ ДЕРЖИТ ЗАКРЫТЫМИ.

1. ОТМЕНА МОЛЧА ОТМЕНЯЛАСЬ. Успешный путь `_run_op_task` давно пишет статус с
   защитой `status NOT IN (терминальные)`, а ПЯТЬ путей возврата операции в
   очередь — нет: `_maybe_requeue`, `_defer_op_for_flood`,
   `_requeue_op_no_accounts`, `_release_op_for_circuit` и провальная ветка
   `_run_op_task` писали `WHERE id=$1`. Владелец нажимал «Отменить», операция в
   этот момент ловила флуд или сетевую ошибку — и её статус переписывался
   обратно в `pending`. Воркер подхватывал её заново, и массовая операция шла
   вторым кругом по тем же аккаунтам после явного «стоп».

2. ПЛАНОВЫЙ РЕСТАРТ СЧИТАЛСЯ ПАДЕНИЕМ. Railway шлёт SIGTERM на каждом деплое, а
   процесс закрывал пул поверх работающих операций. Они оставались в 'running',
   и на старте `_reset_stale_running` тратил на них бюджет живучести
   (`revive_count`), предназначенный для операций, которые РОНЯЮТ воркер. Три
   деплоя за время одной многочасовой рассылки — и здоровая операция падала в
   'failed'. Заодно аренда аккаунтов не снималась, и флот стоял до истечения
   TTL (15 минут): `_WORKER_ID` содержит свежий uuid4, поэтому ветка «аренда
   наша собственная» после рестарта не срабатывает никогда.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OP_WORKER = os.path.join(ROOT, "services", "op_worker.py")


def _source() -> str:
    with open(OP_WORKER, encoding="utf-8") as f:
        return f.read()


# ── Храповик: ни одна запись статуса не идёт мимо защиты ────────────────────

def test_no_status_write_without_terminal_guard():
    """Любой UPDATE, меняющий status очереди, обязан щадить терминальный статус.

    Проверка по исходнику, а не по месту: правило ломается ровно тем, что
    кто-то пишет ещё один «очевидный» `WHERE id=$1`.

    Смотрим ТОЛЬКО на запросы, у которых `status` стоит в части SET. Запросы,
    двигающие прогресс (`SET done_items=...`), к этому правилу отношения не
    имеют, и детектор, который их ловит, сломан — а сломанный детектор чинят
    вместо кода.

    Исключены намеренно: сторожа зависших (`_reset_stale_running`,
    `_watchdog_stale`) и `shutdown` — они фильтруют `status='running'` в самом
    WHERE, то есть терминальную операцию не тронут по построению.
    """
    src = _source()
    offenders = []
    for m in re.finditer(r"UPDATE operation_queue", src):
        seg = src[m.start():m.start() + 900]
        nxt = seg.find("UPDATE operation_queue", 1)
        if nxt > 0:
            seg = seg[:nxt]
        where_at = seg.upper().find("WHERE")
        set_part = seg[:where_at] if where_at > 0 else seg
        if not re.search(r"\bstatus\s*=", set_part):
            continue                      # запрос двигает прогресс, а не статус
        where_part = seg[where_at:where_at + 400] if where_at > 0 else ""
        # Отбор может стоять и ВЫШЕ по тому же запросу — как у поллера, где
        # UPDATE берёт строки из CTE с `status = 'pending'` и FOR UPDATE
        # SKIP LOCKED. Это такой же отбор по не-терминальному статусу, просто
        # выраженный подзапросом, поэтому смотрим и начало литерала запроса.
        lit = src.rfind('"""', 0, m.start())
        prefix = src[lit:m.start()] if lit > 0 and m.start() - lit < 2000 else ""
        has_guard = (
            "sql_terminal_list" in where_part
            or re.search(r"status\s*=\s*'(running|cancelled)'", where_part)
            or re.search(r"status\s*=\s*'pending'", prefix)
        )
        if not has_guard:
            line = src[:m.start()].count("\n") + 1
            offenders.append(f"  строка {line}: {seg.splitlines()[0].strip()}")
    assert not offenders, (
        "запись статуса операции без защиты от терминального — отмена владельца "
        "будет переписана, а операция пойдёт вторым кругом:\n" + "\n".join(offenders)
    )


def test_the_guard_detector_actually_sees_status_writes():
    """Детектор, который ничего не видит, зелёный всегда — правило тогда мертво."""
    src = _source()
    seen = 0
    for m in re.finditer(r"UPDATE operation_queue", src):
        seg = src[m.start():m.start() + 900]
        where_at = seg.upper().find("WHERE")
        set_part = seg[:where_at] if where_at > 0 else seg
        if re.search(r"\bstatus\s*=", set_part):
            seen += 1
    assert seen >= 5, f"детектор нашёл лишь {seen} записей статуса — он сломан"


# ── Поведение: повтор не воскрешает отменённую операцию ─────────────────────

class _Pool:
    """Пул, который отвечает «ноль строк» на UPDATE — как при отменённой операции."""

    def __init__(self, execute_val="UPDATE 0", row=None):
        self.execute_val = execute_val
        self.row = row if row is not None else {"retry_count": 0, "max_retries": 3}
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return self.row

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []

    async def execute(self, query, *args):
        self.queries.append(query)
        return self.execute_val


@pytest.mark.asyncio
async def test_retry_refuses_to_requeue_cancelled_operation():
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 0")
    requeued = await op_worker._maybe_requeue(
        pool, 42, ConnectionError("сеть отвалилась"), {}, "mass_publish"
    )
    assert requeued is False, (
        "повтор отчитался «операция поставлена», хотя UPDATE не тронул ни одной "
        "строки — отменённая операция считалась бы возвращённой в очередь"
    )
    update = [q for q in pool.queries if "UPDATE operation_queue" in q]
    assert update, "повтор не пытался обновить очередь вовсе"
    assert "status NOT IN" in update[0], "у повтора нет защиты от терминального статуса"


@pytest.mark.asyncio
async def test_retry_still_works_for_live_operation():
    """Защита не должна ломать обычный повтор живой операции."""
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 1")
    requeued = await op_worker._maybe_requeue(
        pool, 42, ConnectionError("сеть отвалилась"), {}, "mass_publish"
    )
    assert requeued is True


@pytest.mark.asyncio
async def test_fatal_error_is_not_requeued():
    """Контрольный случай: фатальную ошибку повторять по-прежнему нельзя."""
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 1")
    assert await op_worker._maybe_requeue(
        pool, 42, RuntimeError("AUTH_KEY_UNREGISTERED"), {}, "mass_publish"
    ) is False


# ── Поведение: плановая остановка ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_shutdown_requeues_own_ops_without_spending_revive_budget():
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 2")
    async with op_worker._active_lock:
        op_worker._active_op_ids.update({7, 8})
    try:
        res = await op_worker.shutdown(pool, grace_s=0)
    finally:
        async with op_worker._active_lock:
            op_worker._active_op_ids.clear()
        op_worker._shutting_down = False

    assert res["requeued"] == 2
    update = [q for q in pool.queries if "UPDATE operation_queue" in q]
    assert update, "остановка не вернула операции в очередь — они осели бы сиротами"
    q = update[0]
    assert "status='pending'" in q
    assert "status='running'" in q, (
        "остановка обязана трогать только 'running' — иначе перепишет уже "
        "завершённую или отменённую операцию"
    )
    assert "revive_count" not in q, (
        "плановый рестарт списал бюджет живучести: три деплоя подряд убили бы "
        "здоровую многочасовую операцию"
    )


@pytest.mark.asyncio
async def test_shutdown_releases_leases_instead_of_waiting_for_ttl():
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 1")
    op_worker.init_op_worker_pool(pool)
    async with op_worker._accounts_lock:
        op_worker._accounts_in_use.update({101, 102, 103})
    try:
        res = await op_worker.shutdown(pool, grace_s=0)
    finally:
        async with op_worker._accounts_lock:
            op_worker._accounts_in_use.clear()
        op_worker._shutting_down = False
        op_worker._db_pool = None

    assert res["released"] == 3
    released = [q for q in pool.queries if "in_operation   = FALSE" in q or "in_operation = FALSE" in q]
    assert released, (
        "аренда не снята — после каждого деплоя флот простаивал бы до истечения "
        "TTL, а операции упирались в «нет свободных аккаунтов»"
    )
    assert "op_lease_owner" in released[0], "снимаем чужие аренды — это чужие живые сессии"


@pytest.mark.asyncio
async def test_shutdown_stops_the_poller_from_taking_new_work():
    from services import op_worker

    pool = _Pool(execute_val="UPDATE 0")
    await op_worker.shutdown(pool, grace_s=0)
    try:
        assert op_worker._shutting_down is True
        pool.queries.clear()
        await op_worker._process_pending(pool, object())
        assert not pool.queries, (
            "поллер взял операцию в 'running' на выходе процесса — она осталась бы "
            "сиротой и потратила бы единицу бюджета живучести ни за что"
        )
    finally:
        op_worker._shutting_down = False


def test_main_calls_shutdown_before_closing_the_pool():
    """Уборка по закрытому пулу — это отсутствие уборки."""
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        src = f.read()
    assert "op_worker.shutdown(pool)" in src, "main не сворачивает воркер операций"
    assert src.index("op_worker.shutdown(pool)") < src.index("await pool.close()"), (
        "shutdown вызывается после закрытия пула — уборка не выполнится"
    )
