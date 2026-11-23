"""Флот: честный дележ аккаунтов между параллельными операциями.

Одна операция больше не забирает весь свободный флот, если у владельца идут
другие операции — иначе конкурентные операции падали «все аккаунты заняты».
В одиночку операция берёт весь флот (без потери пропускной способности).
"""
from __future__ import annotations

import asyncio
import re

import pytest

import services.op_worker as ow


@pytest.fixture(autouse=True)
def _legacy_exclusive_mode():
    """Этот файл проверяет ИСКЛЮЧАЮЩИЙ путь (одна операция и одно подключение
    на аккаунт), который работает при `/fleet_parallel off` и
    FLEET_SHARED_SESSIONS=0. По умолчанию режим совместной работы включён
    (services/shared_sessions), его поведение — в test_shared_fleet_sessions.py."""
    from services import shared_sessions as _ss
    _ss.force(False)
    yield
    _ss.force(None)


class _FakePool:
    """Заглушка пула, моделирующая межпроцессную аренду аккаунтов (v179).

    `fetch` повторяет контракт `UPDATE ... RETURNING id` из op_worker._db_claim:
    аренду выдаём на всё, что не держит ДРУГОЙ владелец. Так дележ флота
    проверяется без живой БД, а межпроцессная дизъюнктность — в
    tests/test_account_lease_postgres.py на настоящем Postgres.
    """

    def __init__(self, running, held_by_others: set[int] | None = None):
        self._running = running
        self._held = set(held_by_others or ())

    async def fetchval(self, q, *a):
        return self._running

    async def fetch(self, q, *a):
        ids = list(a[0]) if a else []
        return [{"id": int(i)} for i in ids if int(i) not in self._held]

    async def execute(self, q, *a):
        return "UPDATE"


def _accts(n):
    return [{"id": i} for i in range(1, n + 1)]


@pytest.fixture(autouse=True)
def _isolate_op_worker_globals():
    """Не протекать в другие тесты: сохранить/восстановить глобалы op_worker."""
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    saved_locks = dict(ow._operation_account_locks)
    saved_active = set(ow._active_op_ids)
    saved_started = dict(ow._op_started_mono)
    saved_ledger = dict(ow._claim_ledger)
    ow._op_started_mono.clear()
    ow._claim_ledger.clear()
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    ow._active_op_ids.clear()
    try:
        yield
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)
        ow._operation_account_locks.clear()
        ow._operation_account_locks.update(saved_locks)
        ow._active_op_ids.clear()
        ow._active_op_ids.update(saved_active)
        ow._op_started_mono.clear()
        ow._op_started_mono.update(saved_started)
        ow._claim_ledger.clear()
        ow._claim_ledger.update(saved_ledger)


def _reset():
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    ow._active_op_ids.clear()


def _start_ops(*op_ids: int, age_s: float = 0.0):
    """Операции op_ids только что стартовали в этом процессе (как в _process_pending)."""
    import time as _t
    ow._active_op_ids.clear()
    ow._active_op_ids.update(op_ids)
    for i in op_ids:
        ow._op_started_mono[i] = _t.monotonic() - age_s


def _set_live_ops(n: int):
    """Смоделировать n РЕАЛЬНО живых операций в памяти процесса (делитель fair-share
    считает только их, игнорируя залипшие DB-строки status='running')."""
    ow._active_op_ids.clear()
    ow._active_op_ids.update(range(1000, 1000 + n))


def test_solo_op_claims_whole_free_fleet():
    _reset()
    _set_live_ops(1)                    # только эта операция реально жива
    ow._db_pool = _FakePool(running=1)
    claimed = asyncio.run(ow._claim_available_accounts(101, _accts(9), owner_id=7))
    assert len(claimed) == 9   # в одиночку — весь флот


def test_solo_op_not_starved_by_phantom_running_rows():
    """Регресс: залипшие DB-строки status='running' (упавшие процессы) НЕ должны
    урезать долю одиночной операции. Живых операций в памяти нет → делитель=1."""
    _reset()
    ow._active_op_ids.clear()           # ни одной живой операции в этом процессе
    ow._db_pool = _FakePool(running=5)  # но в БД 5 «фантомных» running-строк
    claimed = asyncio.run(ow._claim_available_accounts(101, _accts(26), owner_id=7))
    assert len(claimed) == 26           # весь флот, а не 26/5≈6 (прежний баг)


def test_three_parallel_ops_split_fleet_disjointly():
    _reset()
    _start_ops(1, 2, 3)                 # три операции стартовали разом
    ow._db_pool = _FakePool(running=3)  # три параллельные операции владельца
    a = asyncio.run(ow._claim_available_accounts(1, _accts(9), owner_id=7))
    b = asyncio.run(ow._claim_available_accounts(2, _accts(9), owner_id=7))
    c = asyncio.run(ow._claim_available_accounts(3, _accts(9), owner_id=7))
    # каждая получила справедливую долю (⌈9/3⌉=3), и доли ДИЗЪЮНКТНЫ
    assert len(a) == 3 and len(b) == 3 and len(c) == 3
    ids = [x["id"] for x in a + b + c]
    assert len(set(ids)) == 9   # ни один аккаунт не выдан двум операциям


def test_second_op_is_not_starved_under_contention():
    _reset()
    _start_ops(1, 2)
    ow._db_pool = _FakePool(running=2)
    a = asyncio.run(ow._claim_available_accounts(1, _accts(10), owner_id=7))
    b = asyncio.run(ow._claim_available_accounts(2, _accts(10), owner_id=7))
    # первая НЕ забрала весь флот → второй досталось (раньше было 0 → «заняты»)
    assert len(a) == 5 and len(b) == 5
    assert not (set(x["id"] for x in a) & set(x["id"] for x in b))


def test_neighbour_that_already_holds_its_accounts_does_not_cut_my_selection():
    """Регресс жалобы «операция захватывает несколько аккаунтов, а работает лишь
    часть». Соседняя операция владельца уже держит СВОИ аккаунты 1..5; эта
    выбрала 6..10, все свободны. Прежний делитель считал соседа претендентом и
    отдавал этой ⌈5/2⌉=3 — два выбранных свободных аккаунта не работали вовсе."""
    _reset()
    _start_ops(1)
    ow._db_pool = _FakePool(running=2)
    held = asyncio.run(ow._claim_available_accounts(1, _accts(5), owner_id=7))
    assert len(held) == 5
    _start_ops(1, 2)                    # вторая стартовала, первая уже с флотом
    mine = [{"id": i} for i in range(6, 11)]
    got = asyncio.run(ow._claim_available_accounts(2, mine, owner_id=7))
    assert [a["id"] for a in got] == [6, 7, 8, 9, 10]


def test_long_running_op_without_accounts_does_not_halve_the_fleet():
    """Рассылка ботом/разбор идут часами и аккаунтов не берут. Пока такая
    операция жила, любая массовая операция владельца получала половину выбора."""
    _reset()
    _start_ops(1, 2, age_s=3600)        # сосед №1 стартовал час назад
    ow._op_started_mono[2] = __import__("time").monotonic()
    ow._db_pool = _FakePool(running=2)
    got = asyncio.run(ow._claim_available_accounts(2, _accts(10), owner_id=7))
    assert len(got) == 10


def test_account_held_by_background_loop_is_waited_for_not_dropped(monkeypatch):
    """Аккаунт, который в момент старта держал призрак/прогрев (секунды), раньше
    выпадал из прогона насовсем. Теперь его ждут и берут."""
    _reset()
    ow._db_pool = _FakePool(running=1)
    monkeypatch.setattr(ow, "_BG_HOLD_WAIT_S", 5.0)
    monkeypatch.setattr(ow, "_BG_HOLD_POLL_S", 0.05)
    ow._accounts_in_use.add(3)          # фоновый цикл держит аккаунт 3

    async def _run():
        async def _bg_done():
            await asyncio.sleep(0.2)
            await ow.release_accounts([3])
        t = asyncio.create_task(_bg_done())
        got = await ow._claim_available_accounts(101, _accts(4), owner_id=7)
        await t
        return got

    got = asyncio.run(_run())
    assert sorted(a["id"] for a in got) == [1, 2, 3, 4]
    assert 3 in ow._operation_account_locks.get(101, set())


def test_account_held_by_another_operation_is_not_waited_for(monkeypatch):
    """Чужую операцию не ждём: она может идти часами. Аккаунт уходит в «не пошли
    в работу» с причиной, а не вешает старт."""
    _reset()
    ow._db_pool = _FakePool(running=1)
    monkeypatch.setattr(ow, "_BG_HOLD_WAIT_S", 30.0)
    ow._accounts_in_use.add(3)
    ow._operation_account_locks[555] = {3}
    import time as _t
    t0 = _t.monotonic()
    got = asyncio.run(ow._claim_available_accounts(101, _accts(4), owner_id=7))
    assert _t.monotonic() - t0 < 2.0
    assert sorted(a["id"] for a in got) == [1, 2, 4]


class _DiagPool(_FakePool):
    async def fetch(self, q, *a):
        if "FROM tg_accounts" in q:
            return [{"id": int(i), "is_active": True, "has_sess": True,
                     "acc_status": "active", "on_cooldown": False}
                    for i in (a[1] if len(a) > 1 else [])]
        return await super().fetch(q, *a)


def test_dropped_selected_accounts_are_named_in_the_summary(monkeypatch):
    """Итог операции обязан сказать, что часть выбранных не работала и почему."""
    _reset()
    ow._db_pool = _FakePool(running=1)
    monkeypatch.setattr(ow, "_BG_HOLD_WAIT_S", 0.0)
    ow._accounts_in_use.add(3)
    ow._operation_account_locks[555] = {3}
    asyncio.run(ow._claim_available_accounts(101, _accts(4), owner_id=7))

    async def _no_quar(pool, ids):
        return set()
    monkeypatch.setattr(ow._infra_mem, "quarantined_accounts", _no_quar)
    note = asyncio.run(ow._claim_shortfall_note(
        _DiagPool(running=1), 7, 101, {"account_ids": [1, 2, 3, 4]}))
    assert "Не пошли в работу 1" in note and "заняты другой операцией" in note
    assert 101 not in ow._claim_ledger      # ледгер снят


def test_no_note_when_every_selected_account_worked():
    _reset()
    ow._db_pool = _FakePool(running=1)
    asyncio.run(ow._claim_available_accounts(101, _accts(4), owner_id=7))
    note = asyncio.run(ow._claim_shortfall_note(
        _FakePool(running=1), 7, 101, {"account_ids": [1, 2, 3, 4]}))
    assert note == ""


def test_run_op_task_reports_shortfall_before_final_status():
    """Строка о недоборе попадает в итог ДО классификации статуса: иначе
    операция с выпавшими аккаунтами уходила владельцу как «выполнена»."""
    import inspect
    src = inspect.getsource(ow._run_op_task)
    i_note = src.index("_claim_shortfall_note(")
    i_cls = src.index("op_status.classify_final(")
    assert i_note < i_cls


def test_min_floor_when_more_ops_than_accounts():
    _reset()
    _set_live_ops(5)                    # больше операций, чем аккаунтов
    ow._db_pool = _FakePool(running=5)
    a = asyncio.run(ow._claim_available_accounts(1, _accts(2), owner_id=7))
    assert len(a) >= ow._MIN_ACCOUNTS_PER_OP   # хотя бы минимум


def test_no_owner_id_keeps_legacy_whole_claim():
    _reset()
    _set_live_ops(3)
    ow._db_pool = _FakePool(running=3)
    claimed = asyncio.run(ow._claim_available_accounts(1, _accts(9)))  # owner_id=None
    assert len(claimed) == 9   # без owner-контекста — прежнее поведение


class _QueryAwarePool(_FakePool):
    """Как _FakePool, но различает запрос числа 'running' и запрос числа
    ПАРКОВАННЫХ ждущих флот операций (acct_wait_since). Нужно для проверки, что
    держащий весь флот уступает долю операции, которая ждёт в 'pending'."""

    def __init__(self, running=0, waiting=0, held_by_others=None):
        super().__init__(running=running, held_by_others=held_by_others)
        self._waiting = waiting

    async def fetchval(self, q, *a):
        if "acct_wait_since" in q:
            return self._waiting
        return self._running


def test_rebalance_sheds_to_a_parked_waiting_op():
    """Регресс «операция #251 не запустилась: все аккаунты 20 мин были заняты».

    Ждущая свободный флот операция паркуется _requeue_op_no_accounts в
    status='pending' с отметкой acct_wait_since — её НЕТ в _active_op_ids и она
    не 'running'. Прежний счётчик _rebalance_claim видел только 'running' среди
    _active_op_ids, поэтому держащий весь флот не уступал ждущему НИЧЕГО, и тот
    крутился в живой очереди до предела (20 мин) и падал «все аккаунты заняты».
    Теперь ждущие учитываются как конкурентный спрос, и держащий отдаёт долю.
    """
    _reset()
    ow._active_op_ids.clear()
    ow._active_op_ids.add(900)          # в памяти только держащая операция
    ow._db_pool = _QueryAwarePool(running=0, waiting=1)  # соперников-'running' нет, но один ждёт
    held = _accts(10)
    ow._accounts_in_use.update(a["id"] for a in held)
    ow._operation_account_locks[900] = {a["id"] for a in held}
    kept = asyncio.run(ow._rebalance_claim(900, 7, held))
    assert len(kept) == 5, (
        f"держащий весь флот обязан уступить половину ждущей (паркованной) "
        f"операции, оставил {len(kept)} из 10")


def test_rebalance_noop_when_no_rivals_and_none_waiting():
    """Без соперников и без ждущих — делить нечего, флот остаётся за операцией."""
    _reset()
    ow._active_op_ids.clear()
    ow._active_op_ids.add(900)
    ow._db_pool = _QueryAwarePool(running=0, waiting=0)
    held = _accts(10)
    ow._accounts_in_use.update(a["id"] for a in held)
    ow._operation_account_locks[900] = {a["id"] for a in held}
    kept = asyncio.run(ow._rebalance_claim(900, 7, held))
    assert len(kept) == 10, "в одиночку и без ждущих флот не дробится"


def test_busy_fleet_requeues_instead_of_failing():
    src = open("services/op_worker.py", encoding="utf-8").read()
    # исполнители при занятом флоте возвращают requeue, а не failed
    assert '"status": "requeue"' in src
    assert src.count('"status": "requeue"') >= 3
    # _run_op_task обрабатывает requeue → мягкий возврат в очередь
    assert 'result.get("status") == "requeue"' in src
    assert "async def _requeue_op_no_accounts" in src
    # ограничение по времени, чтобы не крутиться вечно
    assert "_ACCT_WAIT_MAX_MIN" in src


def test_claim_passes_owner_id_at_call_sites():
    src = open("services/op_worker.py", encoding="utf-8").read()
    # все исполнительные вызовы claim передают owner_id (честный дележ)
    calls = re.findall(r"_claim_available_accounts\(op_id, [a-z_]+(, owner_id)?\)", src)
    # каждый вызов (кроме определения) должен содержать owner_id
    assert src.count("_claim_available_accounts(op_id,") >= 5
    assert src.count(", owner_id)") >= 5


class _CapturePool:
    """Ловит SQL и параметры UPDATE-реконсилера."""
    def __init__(self):
        self.calls = []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "UPDATE 3"


def test_reconciler_frees_only_accounts_no_live_op_holds():
    ow._accounts_in_use.clear()
    ow._accounts_in_use.update({5, 6})   # эти держит живая операция
    pool = _CapturePool()
    asyncio.run(ow._reconcile_in_operation(pool))
    assert pool.calls, "реконсилер должен выполнить UPDATE"
    q, args = pool.calls[0]
    assert "SET in_operation=FALSE" in q
    assert "id <> ALL" in q                       # исключаем занятые
    held = args[0]
    assert set(held) == {5, 6}                    # именно занятые исключены


def test_reconciler_clears_all_when_nothing_held():
    ow._accounts_in_use.clear()
    pool = _CapturePool()
    asyncio.run(ow._reconcile_in_operation(pool))
    q, args = pool.calls[0]
    # ничего не занято → held=None → чистим все залипшие флаги
    assert args[0] is None


def test_reconciler_wired_into_watchdog_cycle():
    """Уборку залипших флагов обязан вызывать круг обслуживания воркера.

    Ищем вызов в самой `run`, а не строку в файле: шаги круга теперь идут
    через `_maintenance_step` (сбой одного сторожа больше не забирает
    остальные), поэтому вызов стоит внутри лямбды, и привязка к точному тексту
    `await _reconcile_in_operation(pool)` краснела бы на каждой такой правке,
    хотя уборка на месте.
    """
    import ast
    import inspect

    src = open("services/op_worker.py", encoding="utf-8").read()
    assert "async def _reconcile_in_operation" in src
    run = ast.parse(inspect.getsource(ow.run)).body[0]
    called = {
        (n.func.attr if isinstance(n.func, ast.Attribute)
         else getattr(n.func, "id", ""))
        for n in ast.walk(run) if isinstance(n, ast.Call)
    }
    assert "_reconcile_in_operation" in called, (
        "уборка залипших флагов занятости выпала из круга обслуживания: "
        "аккаунты-«зомби» молча выпадут из прогрева и ротации")
