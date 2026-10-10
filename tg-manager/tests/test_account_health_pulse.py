"""Волна S/1B: единый риск-пульс аккаунта — кровоток иммунных сигналов.

Иммунные органы (shadowban/anomaly/flood/drift) писали каждый в свою таблицу; не
было единого сигнала, который нервная (op_worker) и кожа (dashboard) читают ПЕРЕД
действием. get_account_health агрегирует их поверх существующих таблиц (read-only,
продюсеры не трогаем); is_account_quarantined — fail-open рефлекс.

Ключевое свойство безопасности: рефлекс FAIL-OPEN — нет сигнала/ошибка → НЕ
блокируем (anti-detection ядро не должно вставать из-за отсутствия данных пульса).
"""
from __future__ import annotations

import asyncio
import os

from services.infra_memory import (
    get_account_health,
    is_account_quarantined,
    quarantined_accounts,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _FakePool:
    def __init__(self, rows=None, val=None, raise_=False):
        self._rows = rows or []
        self._val = val
        self._raise = raise_
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        if self._raise:
            raise RuntimeError("boom")
        return self._rows

    async def fetchval(self, q, *a):
        self.queries.append(q)
        if self._raise:
            raise RuntimeError("boom")
        return self._val


def test_quarantine_is_fail_open():
    async def _run():
        # нет пула / ошибка БД / нет сигнала → НЕ карантин (fail-open)
        assert await is_account_quarantined(None, 1) is False
        assert await is_account_quarantined(_FakePool(raise_=True), 1) is False
        assert await is_account_quarantined(_FakePool(rows=[]), 1) is False
        # подтверждённое серьёзное ограничение → карантин
        assert await is_account_quarantined(
            _FakePool(rows=[{"account_id": 1}]), 1) is True
    asyncio.run(_run())


def test_quarantine_is_asked_once_for_the_whole_list():
    """Гейт стоит перед КАЖДОЙ массовой операцией и на экране флота, то есть
    спрашивают его про список, а не про аккаунт.

    ЧТО БЫЛО: по запросу на аккаунт. На флоте в 200 аккаунтов — 200 round-trip
    подряд перед действием, а через asyncio.gather — 200 одновременных запросов
    в пул из 20 соединений. И гейт живёт под fail-open: свой же запрос,
    упёршийся в таймаут, читается как «аккаунт чист», то есть предохранитель
    отключал себя ровно на том флоте, где нужнее всего.

    ЧТО ТЕПЕРЬ: один запрос на весь список; одиночная проверка — его частный
    случай, поэтому условие карантина у них общее и разъехаться не может.
    """
    async def _run():
        pool = _FakePool(rows=[{"account_id": 12}, {"account_id": 14}])
        got = await quarantined_accounts(pool, [11, 12, 13, 14])
        assert got == {12, 14}
        assert len(pool.queries) == 1, (
            f"на список ушло {len(pool.queries)} запросов вместо одного")
        assert "= ANY(" in pool.queries[0], "список обязан уходить массивом"

        # Fail-open сохранён и в пакетной проверке.
        assert await quarantined_accounts(None, [1]) == set()
        assert await quarantined_accounts(_FakePool(raise_=True), [1]) == set()
        # Пустой список — вообще без запроса.
        empty = _FakePool(rows=[{"account_id": 1}])
        assert await quarantined_accounts(empty, []) == set()
        assert not empty.queries

        # Одиночная проверка ходит той же дверью: один запрос, не два.
        single = _FakePool(rows=[{"account_id": 5}])
        assert await is_account_quarantined(single, 5) is True
        assert len(single.queries) == 1

    asyncio.run(_run())


def test_the_quarantine_condition_lives_in_one_place():
    """Два запроса с переписанным от руки условием однажды разъедутся, и тогда
    продукт возьмёт в работу аккаунт с подтверждённым ограничением."""
    import inspect

    from services import infra_memory

    where = infra_memory._QUARANTINE_WHERE
    assert "risk_cleared_at" in where, (
        "ручной сброс риска обязан учитываться — иначе «кулдаун не сбрасывается»")
    assert "%ban%" in where and "severity='critical'" in where
    batch = inspect.getsource(infra_memory.quarantined_accounts)
    assert "_QUARANTINE_WHERE" in batch
    single = inspect.getsource(infra_memory.is_account_quarantined)
    assert "quarantined_accounts(" in single, (
        "одиночная проверка обязана быть частным случаем пакетной")
    assert "restriction_events" not in single, (
        "у одиночной проверки снова свой запрос — условия разъедутся")


def test_health_scoring_and_summary():
    async def _run():
        rows = [
            {"id": 1, "phone": "a", "acc_status": "active", "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 0},
            {"id": 2, "phone": "b", "acc_status": "active", "trust_score": 1.0, "restrictions": 0, "severe": 1, "floods": 0},
            {"id": 3, "phone": "c", "acc_status": "active", "trust_score": 1.0, "restrictions": 2, "severe": 0, "floods": 0},
            {"id": 4, "phone": "d", "acc_status": "active", "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 1},
        ]
        r = await get_account_health(_FakePool(rows=rows), 99)
        assert r["summary"] == {"healthy": 1, "at_risk": 2, "quarantine": 1, "total": 4}
        assert r["accounts"][0]["status"] == "quarantine"  # худшие вперёд
        # ошибка БД → пустой пульс, не исключение
        empty = await get_account_health(_FakePool(raise_=True), 99)
        assert empty["summary"]["total"] == 0
    asyncio.run(_run())


def test_acc_status_folds_into_health():
    """acc_status тоже сигнал: banned→карантин, cooldown/warming→риск (read-only)."""
    async def _run():
        rows = [
            {"id": 1, "phone": "a", "acc_status": "active", "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 0},
            {"id": 2, "phone": "b", "acc_status": "banned", "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 0},
            {"id": 3, "phone": "c", "acc_status": "warming", "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 0},
        ]
        r = await get_account_health(_FakePool(rows=rows), 99)
        assert r["summary"] == {"healthy": 1, "at_risk": 1, "quarantine": 1, "total": 3}
    asyncio.run(_run())


def test_low_trust_folds_into_health():
    """Единый пульс: низкий trust_score (<0.4) → at_risk, даже без ограничений."""
    async def _run():
        rows = [
            {"id": 1, "phone": "a", "acc_status": "active", "trust_score": 0.9, "restrictions": 0, "severe": 0, "floods": 0},
            {"id": 2, "phone": "b", "acc_status": "active", "trust_score": 0.2, "restrictions": 0, "severe": 0, "floods": 0},
            {"id": 3, "phone": "c", "acc_status": "active", "trust_score": None, "restrictions": 0, "severe": 0, "floods": 0},
        ]
        r = await get_account_health(_FakePool(rows=rows), 99)
        # #1 здоров (trust 0.9), #2 at_risk (trust 0.2), #3 здоров (NULL→1.0)
        assert r["summary"] == {"healthy": 2, "at_risk": 1, "quarantine": 0, "total": 3}
    asyncio.run(_run())


def test_pulse_folds_all_five_signals():
    """Единый пульс сводит ВСЕ сигналы: restriction_events + acc_status + flood +
    trust_score + in-memory account_health.health_score."""
    src = _read("services/infra_memory.py")
    seg = src[src.index("async def get_account_health"):]
    assert "restriction_events" in seg
    assert 'acc_status' in seg
    assert "account_flood_log" in seg
    assert "trust_score" in seg
    assert "account_health" in seg and "health_score" in seg
    # Пороги счётчика проверяем ПОВЕДЕНИЕМ, а не литералом в тексте: сами числа
    # живут в account_health (QUARANTINE_SCORE / RISK_SCORE), чтобы экран снятия
    # пауз и пульс не разъехались, и прежний вариант теста падал бы на любом
    # вынесении константы, ничего не сказав о правиле.
    from services import account_health as _ah

    async def _verdict(score: float) -> str:
        _ah._health_cache.pop(77, None)
        _ah.get_health(77).health_score = score
        rows = [{"id": 77, "phone": "x", "acc_status": "active",
                 "trust_score": 1.0, "restrictions": 0, "severe": 0, "floods": 0}]
        out = await get_account_health(_FakePool(rows=rows), 99)
        _ah._health_cache.pop(77, None)
        return out["accounts"][0]["status"]

    assert asyncio.run(_verdict(_ah.QUARANTINE_SCORE - 1)) == "quarantine"
    assert asyncio.run(_verdict(_ah.RISK_SCORE - 1)) == "at_risk"
    assert asyncio.run(_verdict(100.0)) == "healthy"


def test_reflex_wired_into_op_worker():
    """Рефлекс подключён в горячие пути массовых операций (bulk_join + bulk_leave)."""
    ow = _read("services/op_worker.py")
    assert "is_account_quarantined" in ow
    # именно fail-open вызов через уже импортированный алиас _infra_mem
    assert "_infra_mem.is_account_quarantined(pool" in ow
    # покрыты массовые отправители: join + leave + publish + invite
    assert ow.count("_infra_mem.is_account_quarantined(pool") >= 4
    assert "bulk_leave: аккаунт %s в карантине" in ow
    assert "_exec_mass_publish op=%d: пропущено %d аккаунтов в карантине" in ow
    assert "mass_invite op=%d: пропущено %d аккаунтов в карантине" in ow


def test_operation_bus_supports_label():
    """Волна S/1A: шина умеет писать label — миграция прямых INSERT (которые
    писали label) на operation_bus.submit больше не теряет метку."""
    ob = _read("services/operation_bus.py")
    seg = ob[ob.index("async def submit"):]
    assert "label: Optional[str] = None" in seg
    # label реально пишется в INSERT (а не только принимается)
    ins = seg[seg.index("INSERT INTO operation_queue"):seg.index("RETURNING id")]
    assert "label" in ins
    assert "op_label = label or meta.get(\"description\")" in seg


def test_warmer_skips_session_expired():
    """Волна I (иммунитет→метаболизм): разогрев пропускает session_expired —
    дохлую сессию греть бессмысленно (коннект упадёт).

    Проверяем по СМЫСЛУ, а не по литералу в тексте модуля: набор мёртвых
    статусов теперь один на продукт (`account_status.DEAD_STATUSES`), и
    прежний вариант теста упал бы на любом сведении копий, ничего не сказав
    о самом правиле.
    """
    import inspect

    from services import account_status, account_warmer

    assert "session_expired" in account_status.DEAD_STATUSES
    for impl in (account_warmer._run_daily_warmup_impl,
                 account_warmer._run_warmup_session_impl):
        assert "_acc_status.is_dead(" in inspect.getsource(impl), impl.__name__


def test_pulse_surfaced_in_api_and_ui():
    """Пульс виден: эндпоинт + инъекция в dashboard + плитка «Здоровье»."""
    api = _read("services/mini_app_api.py")
    assert "async def accounts_health" in api
    assert 'add_get("/api/miniapp/accounts/health", accounts_health)' in api
    assert '"account_health": await _account_health_summary' in api
    # Раньше пульс искали по id чипа `dk-health` из второго «Дашборда метрик».
    # Тот экран был недостижим (в него не вёл ни один push) и удалён; живой
    # пульс рисует единый дашборд — и подробнее, тремя состояниями вместо одного
    # числа. Требование прежнее: данные не приходят впустую, экран их показывает.
    from tests.miniapp_source import miniapp_source

    ui = miniapp_source()
    assert "account_health" in ui, "ответ с пульсом никто не читает"
    for state in ("Здоровы", "Под риском", "Карантин"):
        assert state in ui, f"состояние пульса «{state}» нигде не показано"
