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

Архив шифруется AES-256-GCM перед отправкой (см. «Шифрование архива» ниже);
отправка идёт только в заданный владельцем чат (DB_BACKUP_CHAT_ID / первый
ADMIN_IDS). Расшифровать вручную:
    python -m services.db_backup decrypt backup.tar.gz.enc backup.tar.gz
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
# Отправка файла в Telegram: щедрый таймаут (загрузка до 49 МБ) + повторы, чтобы
# сетевой блип не ронял бэкап (инцидент «Request timeout error» при /backup).
_SEND_TIMEOUT_S = 300
_SEND_ATTEMPTS = 3

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


# ── Шифрование архива ─────────────────────────────────────────────────────────
#
# ЗАЧЕМ. Архив уходит документом в Telegram-чат и живёт там бессрочно. Секреты в
# нём и так зашифрованы token_vault, но всё остальное — телефоны, юзернеймы,
# собранные аудитории, переписка, платежи всех владельцев — лежало в CSV
# открытым текстом. Утечка одного сообщения из чата бэкапов (чужой админ в
# канале, угнанный аккаунт владельца, пересылка) отдавала базу целиком.
#
# КАК. AES-256-GCM поверх готового tar.gz. Ключ — DB_BACKUP_KEY, а если он не
# задан, ключ хранилища секретов (TOKEN_ENCRYPTION_KEY и его связка). Отдельный
# DB_BACKUP_KEY лучше: его можно хранить вне Railway (в менеджере паролей) и
# расшифровать бэкап, даже если окружение сервиса потеряно. Восстановление
# понимает и зашифрованные, и старые открытые архивы.
#
# Формат: b"IGBK1" + nonce(12) + tag(16) + ciphertext.
_ENC_MAGIC = b"IGBK1"


def _encrypt_enabled() -> bool:
    return os.getenv("DB_BACKUP_ENCRYPT", "1").strip().lower() not in ("0", "false", "no")


def _backup_keys() -> list[bytes]:
    """Ключи для расшифровки архива: DB_BACKUP_KEY первым, затем связка vault."""
    import hashlib

    keys: list[bytes] = []
    raw = os.getenv("DB_BACKUP_KEY", "").strip()
    if raw:
        keys.append(hashlib.sha256(raw.encode()).digest())
    try:
        from services.token_vault import _decrypt_keys
        keys.extend(k for k in _decrypt_keys() if k not in keys)
    except Exception:
        log.warning("db_backup: связка ключей token_vault недоступна", exc_info=True)
    return keys


def encrypt_archive(archive: bytes) -> bytes:
    """Зашифровать архив первым ключом из _backup_keys()."""
    from Crypto.Cipher import AES as _AES

    keys = _backup_keys()
    if not keys:
        raise RuntimeError("db_backup: нет ключа для шифрования бэкапа")
    nonce = os.urandom(12)
    cipher = _AES.new(keys[0], _AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(archive)
    return _ENC_MAGIC + nonce + tag + ct


def is_encrypted_archive(blob: bytes) -> bool:
    return bool(blob) and blob[:len(_ENC_MAGIC)] == _ENC_MAGIC


def decrypt_archive(blob: bytes) -> bytes:
    """Снять шифр с архива; открытый tar.gz возвращается как есть.

    Бросает ValueError, если ни один ключ не подошёл: молча вернуть мусор в
    restore хуже, чем внятный отказ.
    """
    if not is_encrypted_archive(blob):
        return blob
    from Crypto.Cipher import AES as _AES

    body = blob[len(_ENC_MAGIC):]
    nonce, tag, ct = body[:12], body[12:28], body[28:]
    for key in _backup_keys():
        try:
            return _AES.new(key, _AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
        except Exception:
            continue
    raise ValueError(
        "бэкап зашифрован, но ни DB_BACKUP_KEY, ни TOKEN_ENCRYPTION_KEY (с "
        "TOKEN_ENCRYPTION_KEYS_PREVIOUS) его не открывают — задайте ключ, "
        "которым он был снят")


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
        # Единый снапшот на весь дамп: список таблиц, секвенции и ВСЕ COPY читаются
        # в одной REPEATABLE READ транзакции. Иначе параллельные записи между
        # дампами разных таблиц дают несогласованный архив (child ссылается на
        # parent, которого нет в его дампе). При restore FK-триггеры выключены
        # (session_replication_role=replica), поэтому такой мусор зальётся МОЛЧА.
        # Так же обеспечивает консистентность pg_dump.
        async with conn.transaction(isolation="repeatable_read"):
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
        ext = "tar.gz.enc" if is_encrypted_archive(archive) else "tar.gz"
        name = f"infragram_backup_{ts}.{ext}" if total == 1 \
            else f"infragram_backup_{ts}.{ext}.part{idx:03d}of{total:03d}"
        cap = None
        if idx == 1:
            size_mb = len(archive) / (1024 * 1024)
            cap = (f"🗄 Бэкап БД {ts} UTC\n"
                   f"Размер: {size_mb:.1f} МБ, частей: {total}\n"
                   f"Восстановление: склеить части по порядку → tar.gz → "
                   f"db_backup.restore_archive.")
            if caption_note:
                cap += f"\n{caption_note}"
        # Загрузка документа в Telegram может идти долго (файл до 49 МБ по
        # небыстрой сети Railway↔Telegram), а дефолтный request-таймаут aiogram
        # (~60с) её обрывает — отсюда «Request timeout error». Даём щедрый таймаут
        # и пару повторов с backoff: разовый сетевой блип не должен ронять бэкап.
        last_err: Exception | None = None
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            try:
                await bot.send_document(
                    chat_id, BufferedInputFile(part, filename=name), caption=cap,
                    request_timeout=_SEND_TIMEOUT_S)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < _SEND_ATTEMPTS:
                    log.warning("db_backup: отправка части %d/%d, попытка %d не удалась (%s) — повтор",
                                idx, total, attempt, e)
                    await asyncio.sleep(2 * attempt)
        if last_err is not None:
            raise last_err
        await asyncio.sleep(0.5)  # не упереться в флуд-лимит при многих частях
    return total


# ── Восстановление ─────────────────────────────────────────────────────────────
def parse_archive(archive: bytes) -> tuple[dict, dict[str, bytes]]:
    """Разобрать tar.gz (зашифрованный или открытый) → (manifest, {table: csv_bytes})."""
    archive = decrypt_archive(archive)
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
# ── Проверка архива и учение восстановления ───────────────────────────────────
#
# ЗАЧЕМ. Бэкап, который ни разу не восстанавливали, — гипотеза, а не бэкап.
# После потери базы 30.08.2026 автобэкап появился, но его годность никто не
# проверял: сломанный архив (ошибка шифрования, обрезанная часть, таблица,
# которую COPY не переварит) выяснился бы ровно в день аварии.
#
# Два уровня:
#   * verify_payload — перед КАЖДОЙ отправкой: шифротекст расшифровывается
#     обратно и разбирается. Дешево, ловит порчу архива и ключа.
#   * restore_drill — раз в DB_RESTORE_DRILL_EVERY бэкапов (по умолчанию 28,
#     около недели при шаге 6 ч) архив по-настоящему заливается COPY во
#     временную схему той же базы, и число строк сверяется с CSV. Всё внутри
#     транзакции, которая откатывается: следов в базе не остаётся, отдельная
#     база и права CREATE DATABASE не нужны.


def verify_payload(payload: bytes, archive: bytes) -> dict:
    """Проверить, что отправляемое восстанавливается в исходный архив."""
    plain = decrypt_archive(payload)
    if plain != archive:
        raise ValueError("архив после расшифровки не совпадает с исходным")
    manifest, tables = parse_archive(plain)
    missing = [t for t in manifest.get("tables", []) if t not in tables]
    if missing:
        raise ValueError(f"в архиве нет таблиц из манифеста: {missing[:5]}")
    return {"tables": len(tables)}


def _csv_rows(raw: bytes) -> int:
    """Число строк данных в CSV (с учётом переводов строк внутри кавычек)."""
    import csv

    reader = csv.reader(io.StringIO(raw.decode("utf-8")))
    return max(0, sum(1 for _ in reader) - 1)


async def restore_drill(pool, archive: bytes) -> dict:
    """Учение: залить архив во временную схему и сверить строки. Без следов.

    Возвращает {ok, tables, rows, mismatched: [...], skipped: [...]}.
    """
    _manifest, tables = parse_archive(archive)
    report = {"ok": False, "tables": 0, "rows": 0, "mismatched": [], "skipped": []}
    schema = "restore_drill_tmp"
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            for tbl, raw in tables.items():
                if await conn.fetchval("SELECT to_regclass($1)", f'public."{tbl}"') is None:
                    report["skipped"].append(tbl)
                    continue
                await conn.execute(
                    f'CREATE TABLE "{schema}"."{tbl}" (LIKE public."{tbl}" INCLUDING DEFAULTS)')
                await conn.copy_to_table(tbl, schema_name=schema, source=io.BytesIO(raw),
                                         format="csv", header=True)
                got = await conn.fetchval(f'SELECT count(*) FROM "{schema}"."{tbl}"')
                want = _csv_rows(raw)
                report["tables"] += 1
                report["rows"] += int(got)
                if int(got) != want:
                    report["mismatched"].append(f"{tbl}: {got} из {want}")
            report["ok"] = not report["mismatched"]
        finally:
            await tr.rollback()
    return report


def _drill_every() -> int:
    try:
        return max(0, int(os.getenv("DB_RESTORE_DRILL_EVERY", "28")))
    except ValueError:
        return 28


def _drill_max_bytes() -> int:
    try:
        return int(float(os.getenv("DB_RESTORE_DRILL_MAX_MB", "500")) * 1024 * 1024)
    except ValueError:
        return 500 * 1024 * 1024


# ── Копия вне Telegram: S3-совместимое хранилище ──────────────────────────────
#
# Telegram-чат — единственное внешнее хранилище, и его потеря (бан бота,
# удалённый чат, угнанный аккаунт владельца) уносит все копии разом. Вторая
# копия — в любом S3-совместимом хранилище (AWS S3, Backblaze B2, Cloudflare
# R2, Yandex Object Storage, MinIO). Включается переменными BACKUP_S3_*;
# подпись AWS SigV4 без boto3 (зависимости в проекте курируются).


def _s3_config() -> Optional[dict]:
    endpoint = os.getenv("BACKUP_S3_ENDPOINT", "").strip().rstrip("/")
    bucket = os.getenv("BACKUP_S3_BUCKET", "").strip()
    access = os.getenv("BACKUP_S3_ACCESS_KEY", "").strip()
    secret = os.getenv("BACKUP_S3_SECRET_KEY", "").strip()
    if not (endpoint and bucket and access and secret):
        return None
    return {"endpoint": endpoint, "bucket": bucket, "access": access,
            "secret": secret, "region": os.getenv("BACKUP_S3_REGION", "us-east-1").strip(),
            "prefix": os.getenv("BACKUP_S3_PREFIX", "infragram/").strip()}


def s3_sign_put(cfg: dict, key: str, body: bytes, now: datetime) -> tuple[str, dict]:
    """URL и заголовки PUT-запроса с подписью AWS Signature V4 (path-style)."""
    import hashlib
    import hmac
    from urllib.parse import quote, urlsplit

    u = urlsplit(cfg["endpoint"])
    host = u.netloc
    path = "/" + quote(cfg["bucket"], safe="") + "/" + quote(key, safe="/-_.~")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([
        "PUT", path, "",
        "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
        signed, payload_hash])
    scope = f"{day}/{cfg['region']}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])

    def _h(k: bytes, m: str) -> bytes:
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    k = _h(("AWS4" + cfg["secret"]).encode(), day)
    k = _h(k, cfg["region"])
    k = _h(k, "s3")
    k = _h(k, "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {
        "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={cfg['access']}/{scope}, "
                          f"SignedHeaders={signed}, Signature={sig}"),
    }
    return f"{u.scheme}://{host}{path}", out


async def upload_to_s3(payload: bytes, filename: str) -> Optional[str]:
    """Положить архив в S3-совместимое хранилище. None — не настроено."""
    cfg = _s3_config()
    if cfg is None:
        return None
    import aiohttp

    key = cfg["prefix"] + filename
    url, headers = s3_sign_put(cfg, key, payload, datetime.now(timezone.utc))
    timeout = aiohttp.ClientTimeout(total=_SEND_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.put(url, data=payload, headers=headers) as r:
            if r.status >= 300:
                text = (await r.text())[:300]
                raise RuntimeError(f"S3 ответил {r.status}: {text}")
    return key


async def run_backup_once(pool, bot, *, note: str = "", drill: bool = False) -> dict:
    """Снять, проверить и отправить один бэкап. drill=True — с учением восстановления.

    Возвращает {ok, parts, size, verified, drill?, s3?, error?}.
    """
    chat_id = backup_chat_id()
    if chat_id is None:
        return {"ok": False, "error": "нет назначения: задайте DB_BACKUP_CHAT_ID или ADMIN_IDS"}
    try:
        archive = await create_archive(pool)
    except Exception as e:
        log.warning("db_backup: снятие дампа упало: %s", e, exc_info=True)
        return {"ok": False, "error": f"дамп: {e}"}
    payload = archive
    if _encrypt_enabled():
        try:
            payload = encrypt_archive(archive)
        except Exception as e:
            # Отправить открытым, раз шифрование сломалось, — ровно та утечка,
            # от которой оно защищает. Лучше пропустить бэкап и сказать об этом.
            log.error("db_backup: шифрование архива упало, бэкап НЕ отправлен: %s", e,
                      exc_info=True)
            return {"ok": False, "error": f"шифрование: {e}", "size": len(archive)}
        note = (note + "\n" if note else "") + (
            "🔒 Зашифрован ключом "
            + ("DB_BACKUP_KEY" if os.getenv("DB_BACKUP_KEY", "").strip()
               else "TOKEN_ENCRYPTION_KEY"))
    try:
        verify_payload(payload, archive)
    except Exception as e:
        log.error("db_backup: архив не прошёл проверку восстановимости, НЕ отправлен: %s",
                  e, exc_info=True)
        return {"ok": False, "error": f"проверка архива: {e}", "size": len(archive)}
    result: dict = {"ok": True, "size": len(archive), "verified": True}
    if drill:
        if len(archive) > _drill_max_bytes():
            log.warning("db_backup: учение восстановления пропущено — архив %.0f МБ "
                        "больше DB_RESTORE_DRILL_MAX_MB", len(archive) / 1048576)
            result["drill"] = {"ok": None, "skipped": "size"}
        else:
            try:
                rep = await restore_drill(pool, archive)
                result["drill"] = rep
                if rep["ok"]:
                    note = (note + "\n" if note else "") + (
                        f"🧪 Учение восстановления: {rep['tables']} таблиц, "
                        f"{rep['rows']} строк — ок")
                else:
                    log.error("db_backup: учение восстановления НЕ сошлось: %s",
                              rep["mismatched"][:10])
                    note = (note + "\n" if note else "") + (
                        "⚠️ Учение восстановления: расхождения в "
                        f"{len(rep['mismatched'])} таблицах")
            except Exception as e:
                log.error("db_backup: учение восстановления упало: %s", e, exc_info=True)
                result["drill"] = {"ok": False, "error": str(e)}
                note = (note + "\n" if note else "") + "⚠️ Учение восстановления упало"
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext = "tar.gz.enc" if is_encrypted_archive(payload) else "tar.gz"
        key = await upload_to_s3(payload, f"infragram_backup_{ts}.{ext}")
        if key:
            result["s3"] = key
            note = (note + "\n" if note else "") + "☁️ Копия в S3"
    except Exception as e:
        # Копия в S3 — второй рубеж; её сбой не отменяет отправку в Telegram.
        log.error("db_backup: загрузка в S3 упала: %s", e, exc_info=True)
        result["s3_error"] = str(e)
    try:
        parts = await send_backup(bot, chat_id, payload, caption_note=note)
    except Exception as e:
        log.warning("db_backup: отправка упала: %s", e, exc_info=True)
        return {**result, "ok": bool(result.get("s3")), "error": f"отправка: {e}"}
    log.info("db_backup: бэкап отправлен (%.1f МБ, %d частей) в чат %s",
             len(archive) / (1024 * 1024), parts, chat_id)
    return {**result, "parts": parts}


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
    every = _drill_every()
    n = 0
    while True:
        try:
            # Учение на первом бэкапе после старта и далее раз в `every`.
            await run_backup_once(pool, bot, note="автоматический",
                                  drill=bool(every) and n % every == 0)
            n += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("db_backup: цикл упал", exc_info=True)
        await asyncio.sleep(_INTERVAL_SEC)


def _cli(argv: list[str]) -> int:
    """python -m services.db_backup decrypt <вход> <выход> — снять шифр с архива."""
    if len(argv) != 3 or argv[0] != "decrypt":
        print("использование: python -m services.db_backup decrypt <вход.tar.gz.enc> <выход.tar.gz>")
        return 2
    with open(argv[1], "rb") as f:
        blob = f.read()
    with open(argv[2], "wb") as f:
        f.write(decrypt_archive(blob))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
