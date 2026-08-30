"""Автоматический бэкап БД вне контейнера — защита от потери данных.

ЗАЧЕМ. Инцидент 2026-08-30: Postgres на Railway работал на ЭФЕМЕРНОМ диске (без
тома), и при пересоздании контейнера после сбоя оплаты вся база (5 месяцев данных)
исчезла — ни тома, ни снапшотов, ни второй копии. Чтобы это было невозможно
повторить, система должна сама регулярно снимать бэкап и класть его ЗА ПРЕДЕЛЫ
Railway.

КАК. Логический дамп на чистом asyncpg (COPY ... TO STDOUT по таблицам) — без
внешних бинарников (pg_dump) и без правок Dockerfile, значит без риска сломать
сборку. Дамп упаковывается в tar.gz (manifest.json + data/<table>.csv +
sequences) и отправляется документом в приватный Telegram-канал/чат (внешнее
хранилище, использует уже существующего бота). Крупный архив режется на части
≤49 МБ (лимит Bot API) и склеивается при восстановлении.

Восстановление (services/db_backup.restore_archive) — точная обратная операция:
пересоздать схему миграциями (это делает старт приложения), затем при выключенных
FK-триггерах (session_replication_role=replica) залить данные COPY и поправить
секвенции. Схема воспроизводима из миграций репозитория, поэтому дамп — data-only,
что делает его версионно-независимым.

Ничего секретного в дамп не попадает сверх того, что и так в БД; отправка идёт
только в заданный владельцем чат (DB_BACKUP_CHAT_ID / первый ADMIN_IDS).
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import tarfile
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# ── Конфиг (через env, безопасные значения по умолчанию) ───────────────────────
_INTERVAL_SEC = int(os.getenv("DB_BACKUP_INTERVAL_SEC", str(6 * 3600)))  # каждые 6ч
_PART_LIMIT = 49 * 1024 * 1024   # 49 МБ — под лимит Telegram Bot API (50 МБ)
_ARCHIVE_VERSION = 1

# Таблицы-логи с высоким оборотом и без ценности для восстановления флота —
# по умолчанию исключаем, чтобы бэкап был компактным и быстрым. Переопределяется
# через DB_BACKUP_EXCLUDE (список через запятую).
_DEFAULT_EXCLUDE = {
    "activity_log", "operation_audit", "operation_log", "account_flood_log",
    "dm_campaign_log", "restriction_events", "account_status_events",
    "schema_migrations",
}


def _excluded_tables() -> set[str]:
    raw = os.getenv("DB_BACKUP_EXCLUDE", "").strip()
    if raw:
        return {t.strip() for t in raw.split(",") if t.strip()}
    return set(_DEFAULT_EXCLUDE)


def _enabled() -> bool:
    return os.getenv("DB_BACKUP_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def backup_chat_id() -> Optional[int]:
    """Куда слать дамп: DB_BACKUP_CHAT_ID или первый из ADMIN_IDS."""
    raw = os.getenv("DB_BACKUP_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("db_backup: DB_BACKUP_CHAT_ID не число: %r", raw)
    try:
        from config import ADMIN_IDS
        if ADMIN_IDS:
            return int(ADMIN_IDS[0])
    except Exception:
        pass
    return None


# ── Снятие дампа ───────────────────────────────────────────────────────────────
async def _list_tables(conn, exclude: set[str]) -> list[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    return [r["tablename"] for r in rows if r["tablename"] not in exclude]


async def _dump_sequences(conn) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema='public'")
    out: dict[str, int] = {}
    for r in rows:
        name = r["sequence_name"]
        try:
            val = await conn.fetchval(f'SELECT last_value FROM "{name}"')
            if val is not None:
                out[name] = int(val)
        except Exception:
            log.debug("db_backup: секвенция %s недоступна", name)
    return out


async def create_archive(pool, *, exclude: Optional[set[str]] = None) -> bytes:
    """Снять логический дамп всех таблиц public в tar.gz. Возвращает байты архива.

    Формат: manifest.json (версия, время, список таблиц, счётчики, секвенции) +
    data/<table>.csv (COPY CSV с заголовком). Data-only: схему восстанавливают
    миграции репозитория.
    """
    exclude = exclude if exclude is not None else _excluded_tables()
    buf = io.BytesIO()
    manifest: dict = {
        "version": _ARCHIVE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": "csv",
        "tables": [],
        "row_counts": {},
    }
    async with pool.acquire() as conn:
        tables = await _list_tables(conn, exclude)
        manifest["sequences"] = await _dump_sequences(conn)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for tbl in tables:
                data = io.BytesIO()
                # COPY на стороне сервера — версионно-независимо и потоково.
                await conn.copy_from_query(
                    f'SELECT * FROM "{tbl}"', output=data, format="csv", header=True)
                raw = data.getvalue()
                manifest["tables"].append(tbl)
                # Число строк = строки CSV минус заголовок (грубо, для отчёта).
                manifest["row_counts"][tbl] = max(0, raw.count(b"\n") - 1)
                info = tarfile.TarInfo(name=f"data/{tbl}.csv")
                info.size = len(raw)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(raw))
            mbytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            minfo = tarfile.TarInfo(name="manifest.json")
            minfo.size = len(mbytes)
            minfo.mtime = int(time.time())
            tar.addfile(minfo, io.BytesIO(mbytes))
    return buf.getvalue()


def split_parts(data: bytes, part_size: int = _PART_LIMIT) -> list[bytes]:
    """Порезать архив на части ≤ part_size (для лимита Telegram)."""
    if len(data) <= part_size:
        return [data]
    return [data[i:i + part_size] for i in range(0, len(data), part_size)]


# ── Отправка в Telegram ────────────────────────────────────────────────────────
async def send_backup(bot, chat_id: int, archive: bytes, *, caption_note: str = "") -> int:
    """Отправить архив (с нарезкой на части) документами в чат. Возвращает число частей."""
    from aiogram.types import BufferedInputFile

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parts = split_parts(archive, _PART_LIMIT)
    total = len(parts)
    for idx, part in enumerate(parts, 1):
        name = f"infragram_backup_{ts}.tar.gz" if total == 1 \
            else f"infragram_backup_{ts}.tar.gz.part{idx:03d}of{total:03d}"
        cap = None
        if idx == 1:
            size_mb = len(archive) / (1024 * 1024)
            cap = (f"🗄 Бэкап БД {ts} UTC\n"
                   f"Размер: {size_mb:.1f} МБ, частей: {total}\n"
                   f"Восстановление: склеить части по порядку → tar.gz → "
                   f"db_backup.restore_archive.")
            if caption_note:
                cap += f"\n{caption_note}"
        await bot.send_document(
            chat_id, BufferedInputFile(part, filename=name), caption=cap)
        await asyncio.sleep(0.5)  # не упереться в флуд-лимит при многих частях
    return total


# ── Восстановление ─────────────────────────────────────────────────────────────
def parse_archive(archive: bytes) -> tuple[dict, dict[str, bytes]]:
    """Разобрать tar.gz → (manifest, {table: csv_bytes})."""
    tables: dict[str, bytes] = {}
    manifest: dict = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f is None:
                continue
            content = f.read()
            if member.name == "manifest.json":
                manifest = json.loads(content.decode("utf-8"))
            elif member.name.startswith("data/") and member.name.endswith(".csv"):
                tbl = member.name[len("data/"):-len(".csv")]
                tables[tbl] = content
    return manifest, tables


async def restore_archive(pool, archive: bytes, *, truncate: bool = True) -> dict:
    """Восстановить данные из архива. ВНИМАНИЕ: перезаписывает таблицы из дампа.

    Схема должна уже существовать (её накатывают миграции при старте). Данные
    заливаются при выключенных FK-триггерах (session_replication_role=replica),
    затем поправляются секвенции. Возвращает отчёт {restored, skipped, rows}.
    """
    manifest, tables = parse_archive(archive)
    report = {"restored": [], "skipped": [], "rows": 0}
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET session_replication_role = replica")
            # Какие таблицы из дампа реально есть в схеме.
            present: list[str] = []
            for tbl in tables:
                if await conn.fetchval("SELECT to_regclass($1)", f'public."{tbl}"') is None:
                    report["skipped"].append(tbl)
                else:
                    present.append(tbl)
            # Чистим ВСЕ целевые таблицы ОДНИМ TRUNCATE до заливки — иначе
            # TRUNCATE ... CASCADE одной таблицы затрёт уже восстановленные соседние
            # (parent затирает child по внешнему ключу).
            if truncate and present:
                cols = ", ".join(f'"{t}"' for t in present)
                await conn.execute(f"TRUNCATE TABLE {cols} CASCADE")
            for tbl in present:
                await conn.copy_to_table(
                    tbl, source=io.BytesIO(tables[tbl]), format="csv", header=True)
                report["restored"].append(tbl)
                report["rows"] += int(manifest.get("row_counts", {}).get(tbl, 0))
            # Секвенции — после заливки данных.
            for name, val in (manifest.get("sequences") or {}).items():
                try:
                    await conn.execute(
                        f'SELECT setval($1, $2, true)', name, int(val))
                except Exception:
                    log.debug("restore: секвенция %s не выставлена", name)
            await conn.execute("SET session_replication_role = default")
    return report


# ── Один прогон и фоновый цикл ─────────────────────────────────────────────────
async def run_backup_once(pool, bot, *, note: str = "") -> dict:
    """Снять и отправить один бэкап. Возвращает {ok, parts, size, error?}."""
    chat_id = backup_chat_id()
    if chat_id is None:
        return {"ok": False, "error": "нет назначения: задайте DB_BACKUP_CHAT_ID или ADMIN_IDS"}
    try:
        archive = await create_archive(pool)
    except Exception as e:
        log.warning("db_backup: снятие дампа упало: %s", e, exc_info=True)
        return {"ok": False, "error": f"дамп: {e}"}
    try:
        parts = await send_backup(bot, chat_id, archive, caption_note=note)
    except Exception as e:
        log.warning("db_backup: отправка упала: %s", e, exc_info=True)
        return {"ok": False, "error": f"отправка: {e}", "size": len(archive)}
    log.info("db_backup: бэкап отправлен (%.1f МБ, %d частей) в чат %s",
             len(archive) / (1024 * 1024), parts, chat_id)
    return {"ok": True, "parts": parts, "size": len(archive)}


async def run_backup_loop(pool, bot) -> None:
    """Фоновый цикл автобэкапа. Запускать один раз на процесс (worker-роль)."""
    if not _enabled():
        log.info("db_backup: отключён через DB_BACKUP_ENABLED")
        return
    if backup_chat_id() is None:
        log.warning("db_backup: не задан DB_BACKUP_CHAT_ID и нет ADMIN_IDS — "
                    "автобэкап не запущен (задайте назначение)")
        return
    log.info("db_backup: автобэкап запущен (каждые %dч)", _INTERVAL_SEC // 3600)
    # Первый бэкап — вскоре после старта, но не мгновенно (дать БД подняться).
    await asyncio.sleep(120)
    while True:
        try:
            await run_backup_once(pool, bot, note="автоматический")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("db_backup: цикл упал", exc_info=True)
        await asyncio.sleep(_INTERVAL_SEC)
