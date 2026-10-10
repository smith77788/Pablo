"""Предохранитель операций: автопауза владельца при повторных сбоях.

Выделено из op_worker.py (распил монолита; сам op_worker импортирует эти имена
обратно, контракт op_worker._circuit_breaker_* не меняется).

Если 3+ операций подряд у владельца падают (FloodWait/PeerFlood/…), цепь
«открывается» и все его операции встают на 30 минут.

СОСТОЯНИЕ ОБЩЕЕ ДЛЯ ПРОЦЕССОВ (находка аудита №3). Раньше счётчик жил только в
`_circuit_breaker_state` — словаре уровня модуля. При одном процессе это работало,
но после разделения ролей и с несколькими воркерами предохранитель ломался там,
где он нужнее всего: каждая реплика считала сбои отдельно (порог «3 подряд»
превращался в 3×N), сработавшая пауза не останавливала соседа, а роль web
показывала «всё в порядке», потому что читала свою пустую память.

Теперь источник истины — таблица `op_circuit_breaker` (schema_v182), а словарь
остался КЭШЕМ: он обслуживает синхронных читателей и работает как резерв, если
пула нет вовсе (тесты, одиночный запуск без БД). Пул задаётся через set_pool()
(op_worker.init_op_worker_pool прокидывает его сюда).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

_CIRCUIT_BREAKER_THRESHOLD = 3  # кол-во подряд ошибок для trip
_CIRCUIT_BREAKER_COOLDOWN = 1800  # 30 минут cooldown
_circuit_breaker_state: dict[int, dict] = {}  # owner_id → {failures, tripped_at, cooldown_until}
_circuit_breaker_lock = asyncio.Lock()

# Пул БД для общего (межпроцессного) состояния. None → работаем на кэше в памяти.
_db_pool = None


def set_pool(pool) -> None:
    """Задать пул БД для общего состояния предохранителя."""
    global _db_pool
    _db_pool = pool


def _cb_blank() -> dict:
    return {"failures": 0, "tripped_at": None, "cooldown_until": None}


def _cb_apply(state: dict, success: bool, now: float) -> dict:
    """Чистый переход состояния предохранителя. Мутирует и возвращает state.

    Вынесен отдельно, чтобы БД и память считали ОДНО И ТО ЖЕ: разъехавшаяся
    логика здесь означала бы, что пауза наступает по-разному в зависимости от
    того, доступна ли БД.
    """
    # Cooldown вышел — цепь закрывается сама, счётчик обнуляется.
    if state["cooldown_until"] and now > state["cooldown_until"]:
        state.update(_cb_blank())

    if success:
        state["failures"] = max(0, state["failures"] - 1)  # затухание на успехе
        # Восстановление: если ошибок стало меньше порога — закрываем цепь
        # (не держим паузу до конца cooldown, раз аккаунты снова работают).
        if state["failures"] < _CIRCUIT_BREAKER_THRESHOLD:
            state["tripped_at"] = None
            state["cooldown_until"] = None
        return state

    state["failures"] += 1
    if state["failures"] >= _CIRCUIT_BREAKER_THRESHOLD and not state["tripped_at"]:
        state["tripped_at"] = now
        state["cooldown_until"] = now + _CIRCUIT_BREAKER_COOLDOWN
    return state


def _cb_is_open(state: dict | None, now: float) -> bool:
    if not state or not state.get("tripped_at"):
        return False
    if state.get("cooldown_until") and now > state["cooldown_until"]:
        return False
    return True


def _cb_status(state: dict | None, now: float) -> dict:
    state = state or {}
    if not _cb_is_open(state, now):
        # Цепь закрыта: после истёкшего cooldown счётчик уже неактуален.
        failures = 0 if state.get("tripped_at") else int(state.get("failures") or 0)
        return {"status": "closed", "failures": failures}
    remaining = int(state["cooldown_until"] - now) if state.get("cooldown_until") else 0
    return {"status": "open", "failures": int(state.get("failures") or 0),
            "cooldown_remaining_s": remaining}


def _cb_from_row(row) -> dict:
    """Строка БД → то же представление, что в памяти (epoch-секунды)."""
    def _ts(v):
        return v.timestamp() if v is not None else None
    return {"failures": int(row["failures"] or 0),
            "tripped_at": _ts(row["tripped_at"]),
            "cooldown_until": _ts(row["cooldown_until"])}


async def _cb_db_record(owner_id: int, success: bool) -> dict | None:
    """Записать исход операции в общее состояние. None — БД недоступна.

    Решение принимается под блокировкой строки: два воркера, завершившие
    операции одновременно, дают +2 к счётчику, а не +1 (без блокировки
    «прочитал-посчитал-записал» теряет инкремент соседа — ровно так порог
    и размывался бы обратно).
    """
    if not _db_pool:
        return None
    try:
        async with _db_pool.acquire() as conn:
            async with conn.transaction():
                # Строку создаём заранее: FOR UPDATE не блокирует несуществующую.
                await conn.execute(
                    "INSERT INTO op_circuit_breaker(owner_id) VALUES($1) "
                    "ON CONFLICT (owner_id) DO NOTHING", int(owner_id))
                row = await conn.fetchrow(
                    "SELECT failures, tripped_at, cooldown_until FROM op_circuit_breaker "
                    "WHERE owner_id=$1 FOR UPDATE", int(owner_id))
                state = _cb_from_row(row) if row else _cb_blank()
                _cb_apply(state, success, time.time())
                await conn.execute(
                    """UPDATE op_circuit_breaker
                          SET failures       = $2,
                              tripped_at     = CASE WHEN $3::double precision IS NULL
                                                    THEN NULL ELSE to_timestamp($3) END,
                              cooldown_until = CASE WHEN $4::double precision IS NULL
                                                    THEN NULL ELSE to_timestamp($4) END,
                              updated_at     = now()
                        WHERE owner_id = $1""",
                    int(owner_id), int(state["failures"]),
                    state["tripped_at"], state["cooldown_until"])
                return state
    except Exception as e:
        # FAIL-OPEN, в отличие от аренды аккаунтов: предохранитель лишь ставит
        # паузу. Ошибочная пауза остановила бы всю работу владельца, поэтому при
        # недоступной БД падаем на локальный счётчик, а не на «всё запрещено».
        log.warning("circuit_breaker: состояние в БД недоступно (%s) — считаем в памяти", e)
        return None


async def _cb_db_load(owner_id: int) -> dict | None:
    """Прочитать общее состояние. None — БД недоступна (читатель возьмёт кэш)."""
    if not _db_pool:
        return None
    try:
        row = await _db_pool.fetchrow(
            "SELECT failures, tripped_at, cooldown_until FROM op_circuit_breaker "
            "WHERE owner_id=$1", int(owner_id))
    except Exception as e:
        log.warning("circuit_breaker: чтение состояния не удалось (%s)", e)
        return None
    return _cb_from_row(row) if row else _cb_blank()


async def _circuit_breaker_record(owner_id: int, success: bool) -> bool:
    """Записать исход операции. True — цепь открыта (операции на паузе)."""
    async with _circuit_breaker_lock:
        now = time.time()
        was_open = _cb_is_open(_circuit_breaker_state.get(owner_id), now)
        shared = await _cb_db_record(owner_id, success)
        if shared is not None:
            state = shared                      # источник истины — БД
        else:
            state = _cb_apply(
                _circuit_breaker_state.setdefault(owner_id, _cb_blank()), success, now)
        _circuit_breaker_state[owner_id] = state   # кэш для синхронных читателей
        is_open = _cb_is_open(state, now)
        if is_open and not was_open:
            log.warning(
                "circuit_breaker: TRIPPED for owner=%d after %d failures — pausing %ds",
                owner_id, state["failures"], _CIRCUIT_BREAKER_COOLDOWN,
            )
        return is_open


def _circuit_breaker_is_open(owner_id: int) -> bool:
    """Открыта ли цепь по КЭШУ процесса (синхронный быстрый путь).

    Межпроцессное решение принимает `circuit_breaker_is_open` — эта версия
    остаётся для синхронных вызовов и не видит паузу, поставленную соседом.
    """
    return _cb_is_open(_circuit_breaker_state.get(owner_id), time.time())


def _circuit_breaker_status(owner_id: int) -> dict:
    """Состояние по КЭШУ процесса. Общее — `circuit_breaker_status`."""
    return _cb_status(_circuit_breaker_state.get(owner_id), time.time())


async def circuit_breaker_is_open(owner_id: int) -> bool:
    """Открыта ли цепь по ОБЩЕМУ состоянию (все процессы). Резерв — кэш."""
    shared = await _cb_db_load(owner_id)
    if shared is None:
        return _circuit_breaker_is_open(owner_id)
    _circuit_breaker_state[owner_id] = shared
    return _cb_is_open(shared, time.time())


async def circuit_breaker_status(owner_id: int) -> dict:
    """Состояние предохранителя по ОБЩЕМУ состоянию. Резерв — кэш процесса."""
    shared = await _cb_db_load(owner_id)
    if shared is None:
        return _circuit_breaker_status(owner_id)
    _circuit_breaker_state[owner_id] = shared
    return _cb_status(shared, time.time())
