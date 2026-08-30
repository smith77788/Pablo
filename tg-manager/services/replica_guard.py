"""Страж «одной реплики»: громко предупреждает, если процессов больше одного.

ПОЧЕМУ ЭТО ВАЖНО (ось №3 аудита). Лимиты флуда, потолки параллельности на
владельца и троттлы восстановления держатся в ПАМЯТИ одного процесса (реестр
KNOWN_DIVERGENCE в tests/test_no_new_mutable_globals). На двух репликах каждая
считает лимит независимо → суммарный темп по флоту в N раз выше безопасного →
прямой риск бана. Пока эти лимиты не вынесены в общий слой (БД/Redis), запуск
второй реплики опасен, и это ограничение должно быть ЯВНЫМ, а не выясняться по
факту вылета аккаунтов.

КАК РАБОТАЕТ. Каждый процесс раз в интервал пишет хартбит (worker_id + роль +
last_seen). Guard считает РАЗНЫЕ живые worker_id за окно и, если их больше
одного, пишет log.critical с объяснением. Не роняет процесс: цель — заметность,
а не отказ (падение маскировало бы проблему и ломало бы деплой). Осознанный
мульти-реплика режим отключает предупреждение через INFRAGRAM_ALLOW_MULTI_REPLICA=1
(его ставят, ТОЛЬКО когда лимиты вынесены в общий слой).
"""
from __future__ import annotations

import asyncio
import logging
import os

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Хартбит считается живым это число секунд. Окно детекта — с запасом к интервалу
# биения, чтобы рестарт одной реплики не читался как «две» из-за старой строки.
_ALIVE_WINDOW_SEC = 90
_BEAT_INTERVAL_SEC = 30


def _multi_allowed() -> bool:
    return str(os.getenv("INFRAGRAM_ALLOW_MULTI_REPLICA", "")).strip().lower() in (
        "1", "true", "yes", "on")


async def beat(pool, worker_id: str, role: str = "all") -> None:
    """Записать/обновить хартбит этого процесса. Ошибку не поднимаем — хартбит
    вспомогателен, его сбой не должен ронять процесс."""
    try:
        await pool.execute(
            """INSERT INTO process_heartbeats(worker_id, role, started_at, last_seen)
               VALUES ($1, $2, now(), now())
               ON CONFLICT (worker_id)
               DO UPDATE SET last_seen = now(), role = EXCLUDED.role""",
            worker_id, role)
    except Exception:
        log_exc_swallow(log, "replica_guard.beat")


async def active_replica_count(pool) -> int:
    """Сколько РАЗНЫХ живых процессов бьётся сейчас (по окну last_seen)."""
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM process_heartbeats "
            "WHERE last_seen > now() - make_interval(secs => $1)",
            _ALIVE_WINDOW_SEC)
        return int(n or 0)
    except Exception:
        log_exc_swallow(log, "replica_guard.active_replica_count")
        return 0


async def check_single_replica(pool) -> int:
    """Проверить инвариант «одна реплика». Возвращает число живых реплик.

    Если их больше одной и мульти-режим НЕ разрешён явно — громкий critical с
    объяснением, что именно разъезжается и чем это грозит.
    """
    n = await active_replica_count(pool)
    if n > 1 and not _multi_allowed():
        log.critical(
            "ЗАПУЩЕНО %d РЕПЛИК, но лимиты флуда/параллельности живут в ПАМЯТИ "
            "каждого процесса — суммарный темп по флоту превышает безопасный, это "
            "прямой риск бана аккаунтов. Пока лимиты не вынесены в общий слой, "
            "держите ОДНУ реплику. Осознанный мульти-режим: "
            "INFRAGRAM_ALLOW_MULTI_REPLICA=1.", n)
    return n


async def run_heartbeat_loop(pool, worker_id: str, role: str = "all") -> None:
    """Фоновый цикл: бьёт хартбит и проверяет инвариант одной реплики.

    Свой сбой не роняет процесс (guard вспомогателен), но и не молчит.
    """
    # Первая проверка сразу после первого биения — чтобы предупреждение появилось
    # в самом начале лога запуска, а не через интервал.
    await beat(pool, worker_id, role)
    await check_single_replica(pool)
    while True:
        try:
            await asyncio.sleep(_BEAT_INTERVAL_SEC)
            await beat(pool, worker_id, role)
            await check_single_replica(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc_swallow(log, "replica_guard.run_heartbeat_loop")
