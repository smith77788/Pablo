"""Насыщение пула БД — видно в метриках в момент, когда оно случается.

ЗАЧЕМ. Пул один на процесс (по умолчанию 20 соединений), и его делят бот,
HTTP мини-аппа, исполнитель операций и ~70 фоновых циклов. Когда соединения
кончаются, `pool.fetch` не падает и не пишет в лог — он молча ЖДЁТ, без
потолка (`command_timeout` ожидание соединения не покрывает). Снаружи это
«всё тормозит», а единственная метрика пула (`infragram_db_pool_connections`)
снимается раз в 5 минут и такой всплеск не видит.

Замер на стенде 12.11.2026 (6000 аккаунтов, все циклы): запись снимков
здоровья строкой на аккаунт держала до 28 ждущих соединения, и защитная
запись журнала целей операции ждала пул до 0.5 с. Нашлось это только
профилировщиком; эти метрики показывают то же в проде:

  infragram_db_pool_acquire_wait_seconds_sum / _count  — среднее ожидание;
  infragram_db_pool_slow_acquires_total                — ожиданий дольше 100 мс;
  infragram_db_pool_waiters                             — ждущих прямо сейчас;
  infragram_db_pool_acquire_wait_max_seconds            — худшее ожидание за минуту.

Как: обёртка над `Pool._acquire` экземпляра (через неё идут и `pool.fetch`, и
`async with pool.acquire()`). Это внутренний метод asyncpg, поэтому при его
отсутствии обёртка не ставится, а тест `test_db_pool_saturation_is_visible`
краснеет на обновлении asyncpg, а не в проде.
"""
from __future__ import annotations

import time

import asyncpg.pool as _ap

SLOW_ACQUIRE_S = 0.1
_WINDOW_S = 60.0

# Состояние замера ЭТОГО процесса: сколько ждут сейчас и худшее ожидание в
# текущем окне. У пула asyncpg __slots__, своё поле на нём не завести.
_state = {"waiters": 0, "win_max": 0.0, "win_start": 0.0}


class _MeteredPool(_ap.Pool):
    """Тот же пул asyncpg, только `_acquire` меряет ожидание соединения."""

    __slots__ = ()

    async def _acquire(self, timeout):
        from services import metrics as _m

        t0 = time.perf_counter()
        _state["waiters"] += 1
        _m.gauge("infragram_db_pool_waiters", _state["waiters"])
        try:
            return await super()._acquire(timeout)
        finally:
            _state["waiters"] -= 1
            _m.gauge("infragram_db_pool_waiters", _state["waiters"])
            w = time.perf_counter() - t0
            _m.observe("infragram_db_pool_acquire_wait_seconds", w)
            if w >= SLOW_ACQUIRE_S:
                _m.inc("infragram_db_pool_slow_acquires_total")
            now = time.monotonic()
            if now - _state["win_start"] > _WINDOW_S:
                _state["win_start"] = now
                _state["win_max"] = 0.0
            if w > _state["win_max"]:
                _state["win_max"] = w
                _m.gauge("infragram_db_pool_acquire_wait_max_seconds", w)


def instrument(pool) -> bool:
    """Поставить замер ожидания соединения на пул. True — стоит.

    Класс экземпляра меняется на подкласс без новых полей — раскладка памяти
    та же, поведение то же, плюс замер. Повторный вызов ничего не делает.
    """
    if isinstance(pool, _MeteredPool):
        return True
    if type(pool) is not _ap.Pool or not hasattr(_ap.Pool, "_acquire"):
        return False
    pool.__class__ = _MeteredPool
    return True
