"""Страж «одной реплики»: громко предупреждает, если процессов больше одного.

ПОЧЕМУ ЭТО ВАЖНО (ось №3 аудита). Темп флота и выученная осторожность
аккаунтов с 01.11.2026 общие — лежат в Postgres (services/pacing_store.py),
потолок параллельности на владельца держит SQL поллера. Но очередь всё равно
разбирает ОДИН процесс (аренда ниже): сторожа зависших операций возвращают в
очередь всё, что висит в 'running', то есть и живые операции соседа, а
троттлы восстановления и несколько других счётчиков ещё живут в памяти
(реестр KNOWN_DIVERGENCE в tests/test_no_new_mutable_globals). Пока это так,
лишний исполнитель бесполезен, и это должно быть ЯВНЫМ.

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
import socket
import time
import uuid

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


def _metric(name: str, value: int = 1) -> None:
    """Счётчик, если слой метрик доступен; его отсутствие не ломает стража."""
    try:
        from services import metrics as _m

        _m.inc(name, None, float(value))
    except Exception:
        pass

# Хартбит считается живым это число секунд. Окно детекта — с запасом к интервалу
# биения, чтобы рестарт одной реплики не читался как «две» из-за старой строки.
_ALIVE_WINDOW_SEC = 90
_BEAT_INTERVAL_SEC = 30

# Роли, которые РАЗБИРАЮТ очередь операций, то есть держат лимиты флуда в своей
# памяти. `web` их не держит: под этой ролью фоновые циклы не запускаются вовсе
# (main.py), процесс отдаёт мини-апп и бота. Поэтому штатная раскладка
# `INFRAGRAM_ROLE=web` + `worker` — это ДВЕ реплики и ОДИН исполнитель, и
# предупреждение о риске бана к ней не относится. Раньше окно считало процессы
# всех ролей, и документированная самим проектом безопасная раскладка получала
# log.critical «ЗАПУЩЕНО 2 РЕПЛИК … прямой риск бана» каждые тридцать секунд.
# Ложная тревога здесь дороже молчания: на неё перестают смотреть, и настоящий
# случай двух worker-ов проходит незамеченным.
EXECUTOR_ROLES = ("worker", "all")

# Срок аренды исполнителя. Процесс продлевает её каждый круг цикла воркера
# (~10 с), поэтому одна неудачная попытка ничего не роняет, а умерший держатель
# отпускает очередь не дольше чем через этот срок.
_EXEC_LEASE_TTL_S = 90

# Последняя ДОСТОВЕРНАЯ правда этого процесса о том, у кого аренда исполнителя:
# свой worker_id, если аренда досталась нам, и None, если она чужая или
# отпущена. Нужна ровно для одного решения — что делать на сбое запроса (см.
# fail-open в `acquire_executor_lease`). Блип не меняет держателя, поэтому
# правильный ответ на сбой — повторить последнюю известную правду, а не
# выдумать её.
_lease_held_by: "str | None" = None
# Когда (time.monotonic) аренду в последний раз ДОСТОВЕРНО продлили. Сбой
# запроса держит fail-open держателя только пока срок в базе ещё не мог
# истечь: дальше аренду законно забирает другой процесс, и держатель, всё ещё
# считающий себя держателем, работал бы вторым исполнителем.
_lease_confirmed_at: float = 0.0
# Запас до истечения срока в базе: часы базы и процесса, задержка запроса.
_EXEC_LEASE_MARGIN_S = 15


# Имя этого процесса для аренд — ОДНО на процесс.
#
# Аренду исполнителя спрашивал только `op_worker.run`, своим `_WORKER_ID`. Как
# только её начинает спрашивать второй цикл того же процесса (опрос ботов в
# `auto_responder`), два разных имени в одном процессе начали бы отбирать
# аренду друг у друга по кругу. Поэтому имя живёт здесь, а `op_worker` берёт
# его отсюда же.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def holds_executor_lease() -> bool:
    """Держит ли аренду исполнителя ЭТОТ процесс — без запроса к базе.

    Нужно для циклов, которые идут чаще, чем стоит спрашивать базу: быстрый
    опрос «горячих» ботов в `auto_responder` идёт раз в секунду. Отвечаем по
    последней достоверной правде и только пока срок в базе ещё не мог
    истечь — иначе процесс, потерявший аренду, продолжал бы считать себя
    держателем и опрашивать ботов вторым.
    """
    if not _lease_held_by:
        return False
    return (time.monotonic() - _lease_confirmed_at) < (
        _EXEC_LEASE_TTL_S - _EXEC_LEASE_MARGIN_S)


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


async def active_replica_count(pool, roles: "tuple[str, ...] | None" = None) -> int:
    """Сколько РАЗНЫХ живых процессов бьётся сейчас (по окну last_seen).

    `roles` ограничивает счёт ролями; по умолчанию считаются ИСПОЛНИТЕЛИ
    (EXECUTOR_ROLES) — именно они держат лимиты флуда в своей памяти. Передать
    пустой кортеж нельзя: это «не считать никого». Для полного счёта процессов
    всех ролей вызывайте с `roles=()` → None не подходит, поэтому полный счёт
    делает `active_process_count`.
    """
    use = tuple(roles) if roles else EXECUTOR_ROLES
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM process_heartbeats "
            "WHERE last_seen > now() - make_interval(secs => $1) "
            "AND role = ANY($2::text[])",
            _ALIVE_WINDOW_SEC, list(use))
        return int(n or 0)
    except Exception:
        log_exc_swallow(log, "replica_guard.active_replica_count")
        return 0


async def active_process_count(pool) -> int:
    """Живые процессы ВСЕХ ролей — для сводок, а не для предупреждения о банах."""
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM process_heartbeats "
            "WHERE last_seen > now() - make_interval(secs => $1)",
            _ALIVE_WINDOW_SEC)
        return int(n or 0)
    except Exception:
        log_exc_swallow(log, "replica_guard.active_process_count")
        return 0


async def acquire_executor_lease(pool, worker_id: str, role: str = "all") -> bool:
    """Взять или продлить аренду исполнителя. True — очередь разбираем МЫ.

    Одна строка (id=1) на всю установку: держатель разбирает очередь, остальные
    ждут. Берём её, если она свободна, просрочена (держатель умер) или уже наша.

    Сбой запроса — fail-open, но ТОЛЬКО для держателя. Для него причина прежняя:
    без БД процесс всё равно ничего не сделает, а пауза исполнителя из-за
    сетевого блипа останавливает ВСЕ операции продукта. Блип при этом не меняет
    того, кто держит аренду, поэтому держатель просто продолжает себя считать
    держателем.

    Процессу, который про свою аренду ничего не знает (только запустился, или
    ему уже отказали), тот же fail-open стоит совсем другого. Первое действие
    держателя — `_reset_stale_running`, то есть
    `UPDATE operation_queue … WHERE status='running'`: ровно то, от чего аренда
    и защищает (см. докстринг модуля). Значит блип на первой попытке нового
    процесса — это не «краткий разъезд лимитов», а перезапуск живых операций
    соседа: те же приглашения тем же людям, второй пост в канал, обнулённый
    прогресс и потраченная единица бюджета живучести. И случается это на
    деплое, когда старый процесс ещё работает, то есть именно тогда, когда
    опаснее всего. Поэтому такой процесс очередь не берёт и ждёт следующего
    круга (~10 с) — для него это и есть правильная цена.

    Сбой в любом случае не молчит: log.warning и счётчик.
    """
    global _lease_held_by, _lease_confirmed_at
    try:
        row = await pool.fetchrow(
            """INSERT INTO executor_lease(id, worker_id, role, expires_at)
               VALUES (1, $1, $2, now() + make_interval(secs => $3))
               ON CONFLICT (id) DO UPDATE
                  SET worker_id = $1,
                      role = $2,
                      acquired_at = CASE WHEN executor_lease.worker_id = $1
                                         THEN executor_lease.acquired_at ELSE now() END,
                      renewed_at = now(),
                      expires_at = now() + make_interval(secs => $3)
                WHERE executor_lease.worker_id = $1
                   OR executor_lease.expires_at < now()
               RETURNING worker_id""",
            worker_id, role, float(_EXEC_LEASE_TTL_S))
        ours = bool(row and row["worker_id"] == worker_id)
        _lease_held_by = worker_id if ours else None
        if ours:
            _lease_confirmed_at = time.monotonic()
        return ours
    except Exception:
        log_exc_swallow(log, "replica_guard.acquire_executor_lease")
        _metric("infragram_executor_lease_errors_total")
        if _lease_held_by == worker_id:
            if (time.monotonic() - _lease_confirmed_at
                    < _EXEC_LEASE_TTL_S - _EXEC_LEASE_MARGIN_S):
                return True
            # Продлить не удаётся так долго, что срок в базе мог истечь и
            # аренду мог взять другой процесс: дальше работать «держателем»
            # значит быть вторым исполнителем. Отпускаем до следующего
            # достоверного ответа.
            _lease_held_by = None
            log.warning(
                "replica_guard: аренду исполнителя не продлить дольше её срока — "
                "очередь отпускаем, пока база не ответит.")
            return False
        log.warning(
            "replica_guard: аренду исполнителя прочитать не удалось, а этот "
            "процесс её не держит — очередь не берём до следующего круга. Так "
            "уборка зависших операций не сбросит живые операции другого "
            "процесса.")
        return False


async def release_executor_lease(pool, worker_id: str) -> None:
    """Отпустить аренду при плановой остановке — ТОЛЬКО свою.

    Без этого следующий процесс ждёт истечения срока: деплой Railway означал бы
    паузу очереди до полутора минут на каждом выпуске.
    """
    global _lease_held_by
    if _lease_held_by == worker_id:
        # Отпускаем до запроса: даже если UPDATE не дойдёт, себя держателем мы
        # больше не считаем, и сбой следующего запроса не вернёт нам очередь.
        _lease_held_by = None
    try:
        await pool.execute(
            "UPDATE executor_lease SET expires_at = now() - make_interval(secs => 1) "
            "WHERE id = 1 AND worker_id = $1",
            worker_id)
    except Exception:
        log_exc_swallow(log, "replica_guard.release_executor_lease")


async def executor_lease_holder(pool) -> "str | None":
    """worker_id действующего держателя аренды, либо None."""
    try:
        row = await pool.fetchrow(
            "SELECT worker_id FROM executor_lease WHERE id = 1 AND expires_at > now()")
        return str(row["worker_id"]) if row else None
    except Exception:
        log_exc_swallow(log, "replica_guard.executor_lease_holder")
        return None


async def check_single_replica(pool) -> int:
    """Проверить инвариант «одна реплика». Возвращает число живых реплик.

    Если их больше одной и мульти-режим НЕ разрешён явно — громкий critical с
    объяснением, что именно разъезжается и чем это грозит.
    """
    n = await active_replica_count(pool)
    if n > 1 and not _multi_allowed():
        holder = await executor_lease_holder(pool)
        log.critical(
            "ЗАПУЩЕНО %d ПРОЦЕССОВ-ИСПОЛНИТЕЛЕЙ (роль worker/all). Очередь "
            "разбирает ОДИН: аренда исполнителя у %s, остальные ждут — "
            "операции не удваиваются, темп флота общий (в базе), но лишние "
            "процессы бесполезны и держат соединения — оставьте ОДНУ реплику "
            "с ролью worker. Осознанный мульти-режим: INFRAGRAM_ALLOW_MULTI_REPLICA=1.",
            n, holder or "никого (ещё не взята)")
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
