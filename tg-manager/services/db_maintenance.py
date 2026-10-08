"""Database Maintenance — data retention and index health.

Runs every 6 hours. Prunes append-only tables that have no TTL:
  - behavioral_events       → keep 90 days
  - operation_log           → keep 30 days  (per-step logs of operations)
  - restriction_events      → keep 90 days
  - account_flood_log       → keep 30 days
  - search_rankings         → keep 90 days  (trend data for charts)
  - search_snapshots        → keep 14 days  (raw JSON, very heavy)
  - operation_queue done    → keep 30 days  (completed/failed/cancelled/skipped)

Without this, a platform with 50 active users generating ~200 events/day fills
behavioral_events with 900 000+ rows in 90 days and search_snapshots can hit
gigabytes within months.

Удаление идёт ПАЧКАМИ (см. `_prune_batched`). Один безлимитный
`DELETE ... WHERE дата < ...` на большой таблице — это одна транзакция на
миллионы строк: она держит блокировки и раздувает WAL, а если не успеет
(таймаут запроса, редеплой Railway, рестарт контейнера), Postgres откатывает
её целиком. Тогда не удаляется НИЧЕГО и таблица растёт бессрочно, при этом в
логе видно лишь warning раз в шесть часов — снаружи уборка выглядит рабочей.
Пачка = отдельная транзакция: удалённое остаётся удалённым, а хвост, который
не влез в проход, уйдёт на следующем.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

_RETENTION: list[tuple[str, str, str]] = [
    # (table, timestamp_column, interval)
    ("behavioral_events", "occurred_at", "90 days"),
    ("operation_log", "created_at", "30 days"),
    ("restriction_events", "created_at", "90 days"),
    ("account_flood_log", "created_at", "30 days"),
    ("search_rankings", "checked_at", "90 days"),
    ("search_snapshots", "captured_at", "14 days"),
    # EPOCH VI tables
    ("recovery_events", "created_at", "30 days"),
    ("anomaly_events", "detected_at", "14 days"),
    ("system_health_snapshots", "snapshot_at", "7 days"),
    ("infrastructure_alerts", "first_seen_at", "30 days"),
    # Proxy telemetry
    ("proxy_quality_log", "checked_at", "30 days"),
    ("proxy_health_log", "checked_at", "30 days"),
    # Operation audit
    ("operation_audit", "occurred_at", "60 days"),
    # Activity log (UI events) — keep 14 days
    ("activity_log", "occurred_at", "14 days"),
    # История постов редактора канала: антиповтору нужны десятки последних
    # постов, а тело каждого поста на каждый канал весит до 8 КБ.
    ("va_channel_posts", "published_at", "90 days"),
    # Виртуальный администратор: журнал действий, отработанный контент-план и
    # решённые черновики нужны на экране за последние недели, не навсегда.
    ("va_admin_events", "created_at", "60 days"),
    ("va_admin_plan", "created_at", "60 days"),
    ("va_admin_drafts", "created_at", "90 days"),
    # Учёт запросов к ИИ по дням и истёкшие паузы моделей (schema_v239).
    ("llm_usage", "day", "90 days"),
    ("llm_cooldowns", "until", "7 days"),
    # Журналы прогрева: строка на каждое действие каждого аккаунта, пишутся
    # непрерывно фоном. Экраны показывают последние 20-60 записей и сводку за
    # неделю, поэтому месяца истории хватает с запасом. Индексы — schema_v227.
    ("account_warmup_log", "performed_at", "30 days"),
    ("warmup_session_log", "performed_at", "30 days"),
    ("resource_activity_log", "performed_at", "30 days"),
]

_BROADCAST_LOG_RETENTION = "90 days"
# Терминальные статусы рассылки: у идущей журнал доставки нужен для
# возобновления после падения, её не трогаем.
_BROADCAST_DONE_STATUSES = ("done", "completed", "failed", "cancelled", "partial")
# Сколько рассылок закрываем за один проход: уборка идёт раз в шесть часов, а
# пачка держит журнал в узде, не занимая соединение надолго.
_BROADCAST_BATCH = 200

_OPERATION_QUEUE_RETENTION = "30 days"
# "partial" — такой же терминальный статус, как done/failed
# (services/op_status.py). Без него частично выполненные операции НИКОГДА не
# вычищались: их строки копились в operation_queue бессрочно.
_DONE_STATUSES = ("done", "partial", "failed", "cancelled", "skipped", "missed")

# Размер пачки. Одна пачка — одна транзакция; 5000 строк удаляются за доли
# секунды даже на медленном диске, но при этом их не миллион под блокировкой.
_BATCH = 5_000
# Потолок на таблицу за один проход. Гигантский накопленный хвост не должен
# занимать соединение часами: остаток уйдёт через 6 часов следующим проходом.
_MAX_PER_TABLE = 500_000
# Пауза между пачками — вернуть соединение пулу, чтобы уборка не мешала боту.
_BATCH_PAUSE = 0.2


async def _prune_batched(
    pool: asyncpg.Pool, table: str, where: str, *args: object
) -> tuple[int, Exception | None]:
    """Удаляет строки пачками по `_BATCH`. Возвращает (сколько удалено, ошибка).

    Выборка идёт по ctid — физическому адресу строки: подзапрос с LIMIT
    отбирает адреса, DELETE удаляет ровно их. Это стандартный способ
    ограничить DELETE в Postgres (у самого DELETE нет LIMIT).

    Ошибка не выбрасывается наружу: уже удалённые пачки зафиксированы, и вызов
    честно сообщает и их число, и причину остановки.
    """
    sql = (
        f"WITH d AS (DELETE FROM {table} WHERE ctid IN ("
        f"SELECT ctid FROM {table} WHERE ({where}) LIMIT {_BATCH}"
        f") RETURNING 1) SELECT COUNT(*) FROM d"
    )
    total = 0
    while total < _MAX_PER_TABLE:
        try:
            n = int(await pool.fetchval(sql, *args) or 0)
        except Exception as e:  # noqa: BLE001 — причину отдаём вызывающему
            return total, e
        total += n
        if n < _BATCH:
            return total, None
        await asyncio.sleep(_BATCH_PAUSE)

    log.warning(
        "db_maintenance: %s — упёрлись в потолок %d строк за проход, "
        "остаток уйдёт следующим проходом",
        table,
        _MAX_PER_TABLE,
    )
    return total, None


def _record(results: dict[str, int], key: str, deleted: int, err: Exception | None) -> int:
    """Кладёт результат уборки одной таблицы. -1 — не удалено ничего из-за ошибки."""
    if err is not None:
        log.warning("db_maintenance: failed to prune %s: %s", key, err)
        results[key] = deleted if deleted else -1
    else:
        results[key] = deleted
    return deleted


async def _prune_broadcast_delivery_log(pool: asyncpg.Pool) -> tuple[int, Exception | None]:
    """Удалить журнал доставки старых рассылок, ПОМЕТИВ их закрытыми.

    broadcast_delivery_log — строка на каждого получателя каждой рассылки, и он
    не чистился вообще: бот с десятью тысячами подписчиков оставляет десять
    тысяч строк на каждую рассылку навсегда.

    Чистить по сроку напрямую нельзя: «отправить недоставленным» считает
    недоставленными тех, кого НЕТ в журнале, поэтому пустой журнал означает
    повторную отправку ВСЕМ подписчикам — живым людям придёт спам. Поэтому
    сначала ставим рассылке отметку delivery_log_pruned, и только потом удаляем
    её строки. Порядок именно такой: если проход прервётся между отметкой и
    удалением, повторная отправка будет запрещена по рассылке, журнал которой
    ещё цел — сторона безопасная. Обратный порядок означал бы спам.

    Берём только завершённые рассылки: у идущей журнал нужен для возобновления
    после падения (broadcaster читает его на старте, чтобы не отправить дважды).
    """
    try:
        ids = [r["id"] for r in await pool.fetch(
            f"""SELECT id FROM broadcasts
                 WHERE delivery_log_pruned = FALSE
                   AND status = ANY($1::text[])
                   AND COALESCE(finished_at, created_at)
                       < NOW() - INTERVAL '{_BROADCAST_LOG_RETENTION}'
                 ORDER BY id
                 LIMIT {_BROADCAST_BATCH}""",
            list(_BROADCAST_DONE_STATUSES),
        )]
    except Exception as e:  # noqa: BLE001 — причину отдаём вызывающему
        return 0, e
    if not ids:
        return 0, None
    try:
        await pool.execute(
            "UPDATE broadcasts SET delivery_log_pruned = TRUE WHERE id = ANY($1::bigint[])",
            ids,
        )
    except Exception as e:  # noqa: BLE001
        return 0, e
    return await _prune_batched(
        pool, "broadcast_delivery_log", "broadcast_id = ANY($1::bigint[])", ids,
    )


async def run_once(pool: asyncpg.Pool) -> dict[str, int]:
    """Execute one maintenance pass. Returns {table: rows_deleted}."""
    results: dict[str, int] = {}

    for table, ts_col, interval in _RETENTION:
        deleted, err = await _prune_batched(
            pool, table, f"{ts_col} < NOW() - INTERVAL '{interval}'"
        )
        _record(results, table, deleted, err)
        if deleted:
            log.info(
                "db_maintenance: pruned %d rows from %s (>%s)", deleted, table, interval
            )

    # Orphaned infra_memory rows — rows whose account/proxy no longer exists.
    # infra_memory_accounts has no FK to tg_accounts, so deleted accounts leave
    # dangling rows. Clean them up; also prune rows inactive for >90 days.
    deleted, err = await _prune_batched(
        pool,
        "infra_memory_accounts",
        "NOT EXISTS (SELECT 1 FROM tg_accounts WHERE id = infra_memory_accounts.account_id) "
        "   OR updated_at < NOW() - INTERVAL '90 days'",
    )
    _record(results, "infra_memory_accounts(orphan)", deleted, err)
    if deleted:
        log.info(
            "db_maintenance: pruned %d orphaned/stale rows from infra_memory_accounts",
            deleted,
        )

    try:
        # Таблица памяти хранит только необратимые fingerprints: credentials не
        # дублируются в статистике и не попадают в диагностические запросы.
        from services.token_vault import proxy_fingerprint

        _user_rows = await pool.fetch("SELECT proxy_url FROM user_proxies")
        _active = {
            proxy_fingerprint(r["proxy_url"]) for r in _user_rows if r["proxy_url"]
        }
        _im_urls = await pool.fetch("SELECT DISTINCT proxy_url FROM infra_memory_proxies")
        _orphans = [
            r["proxy_url"]
            for r in _im_urls
            if r["proxy_url"] not in _active
            and proxy_fingerprint(r["proxy_url"]) not in _active
        ]
    except Exception as e:
        log.warning(
            "db_maintenance: failed to prune infra_memory_proxies: %s",
            type(e).__name__,
        )
        results["infra_memory_proxies(orphan)"] = -1
    else:
        deleted, err = await _prune_batched(
            pool,
            "infra_memory_proxies",
            "proxy_url = ANY($1::text[]) "
            "   OR updated_at < NOW() - INTERVAL '90 days'",
            _orphans,
        )
        _record(results, "infra_memory_proxies(orphan)", deleted, err)
        if deleted:
            log.info(
                "db_maintenance: pruned %d orphaned/stale rows from infra_memory_proxies",
                deleted,
            )

    # Orphaned account-scoped state/stats — эти таблицы ссылаются на tg_accounts.id
    # БЕЗ внешнего ключа, поэтому удаление аккаунта (mini_app account_delete / bulk
    # delete) оставляет висячие строки. Consumers их не обрабатывают (join на
    # tg_accounts отфильтровывает), но без чистки они копятся бесконечно. У
    # operation_audit своё истечение (>30 дн), infra_memory чистится выше —
    # поэтому здесь только эти три. managed_channels НЕ трогаем: канал — отдельная
    # владеемая сущность, его судьба при удалении аккаунта — продуктовое решение.
    for _tbl, _col in (
        ("account_status_events", "acc_id"),
        ("account_daily_stats", "account_id"),
        ("account_rehab_state", "acc_id"),
    ):
        deleted, err = await _prune_batched(
            pool,
            _tbl,
            f"NOT EXISTS (SELECT 1 FROM tg_accounts a WHERE a.id = {_tbl}.{_col})",
        )
        _record(results, f"{_tbl}(orphan)", deleted, err)
        if deleted:
            log.info("db_maintenance: pruned %d orphaned rows from %s", deleted, _tbl)

    # Журнал доставки рассылок: с отметкой «закрыта», иначе повторная отправка
    # уйдёт всем подписчикам заново (подробности в _prune_broadcast_delivery_log).
    deleted, err = await _prune_broadcast_delivery_log(pool)
    _record(results, "broadcast_delivery_log", deleted, err)
    if deleted:
        log.info(
            "db_maintenance: pruned %d delivery rows from broadcast_delivery_log (>%s)",
            deleted,
            _BROADCAST_LOG_RETENTION,
        )

    # Completed operation_queue entries — but only if operation_log entries are
    # also gone (FK safety: operation_log.op_id refs operation_queue.id).
    # We prune operation_log first (above), then queue entries.
    # Срок — литералом в запрос, как у остальных таблиц выше. Через параметр
    # ($2::INTERVAL) это НЕ работало: asyncpg выводит тип параметра из запроса и
    # на строке '30 days' падает ещё до Postgres — «invalid input for query
    # argument». Уборка завершённых операций из-за этого не работала ни разу:
    # ошибка уходила в лог как «failed to prune operation_queue(done)», а
    # таблица со всеми операциями продукта росла без потолка.
    deleted, err = await _prune_batched(
        pool,
        "operation_queue",
        "status = ANY($1::text[]) "
        f"  AND created_at < NOW() - INTERVAL '{_OPERATION_QUEUE_RETENTION}' "
        "  AND NOT EXISTS ("
        "      SELECT 1 FROM operation_log WHERE op_id = operation_queue.id"
        "  )",
        list(_DONE_STATUSES),
    )
    _record(results, "operation_queue(done)", deleted, err)
    if deleted:
        log.info(
            "db_maintenance: pruned %d completed operations from operation_queue (>%s)",
            deleted,
            _OPERATION_QUEUE_RETENTION,
        )

    return results


async def run(pool: asyncpg.Pool, *, interval_hours: float = 6.0) -> None:
    """Background loop: run maintenance every interval_hours."""
    log.info("db_maintenance: started (interval=%gh)", interval_hours)
    # Initial delay — let the bot warm up before touching the DB
    await asyncio.sleep(300)

    while True:
        try:
            results = await run_once(pool)
            total = sum(v for v in results.values() if v > 0)
            if total:
                log.info("db_maintenance: total %d rows pruned this pass", total)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("db_maintenance: unexpected error: %s", e, exc_info=True)

        await asyncio.sleep(interval_hours * 3600)
