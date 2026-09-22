"""PostgreSQL database layer for Infragram.

Provides connection pool management, schema migrations, and query helpers
for all database operations.

Usage:
    from database.db import create_pool

    pool = await create_pool()
    user = await pool.fetchval("SELECT * FROM users WHERE id=$1", user_id)
"""

import asyncio
import asyncpg
import glob
import json
import logging
import os
import re
import time
from config import DATABASE_URL, PLAN_PRICES_USD, PERIOD_DISCOUNTS

from services.logger import log_exc_swallow, timed
from database.pool_config import get_pool_config, log_pool_config

log = logging.getLogger(__name__)


def migration_version_key(path: str) -> int:
    """Номер миграции для сортировки — цифры сразу после `_v`, а не все цифры имени.

    Раньше склеивались ВСЕ цифры имени, и schema_v190_geo_country_iso2.sql
    превращался в 1902 — уезжал в конец очереди, после v200. Той миграции
    повезло: это идемпотентный бэкфилл данных, порядок ей безразличен. Но
    следующая же с цифрой в описании (v201_ipv6…, v205_2fa…) встала бы не на
    своё место, и если она заводит таблицу, на которую опирается более поздняя,
    — та упадёт при старте.

    Журнал schema_migrations ключуется по ИМЕНИ ФАЙЛА, поэтому смена порядка
    ничего не переприменяет: всё уже применённое так и останется пропущенным.
    """
    name = os.path.basename(path)
    if name == "schema.sql":
        return 0
    m = re.match(r"schema_v(\d+)", name)
    if m:
        return int(m.group(1))
    digits = "".join(filter(str.isdigit, name))
    return int(digits) if digits else 0


def ordered_migration_files(paths: list[str]) -> list[str]:
    """Порядок применения миграций: по номеру версии, при равном — по имени.

    Одного номера версии недостаточно: девять пар файлов делят номер (v146,
    v185, v193, v200, v201, v206, v211, v212, v213), а сортировка в Python
    устойчивая — при равных ключах сохраняется исходный порядок, то есть тот,
    в каком файлы вернула файловая система. На поднятой базе это незаметно
    (журнал schema_migrations ключуется по имени файла, применённое не
    переприменяется), но чистая база прокатывает всю историю, и порядок DDL
    оказывался разным на разных машинах. Имя файла во втором элементе ключа
    делает порядок одинаковым везде.

    Дедуп по базовому имени: файл ищется и в корне, и в database/, и одно имя
    не должно примениться дважды.
    """
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(paths, key=lambda p: (migration_version_key(p), os.path.basename(p))):
        name = os.path.basename(path)
        if name in seen:
            continue
        seen.add(name)
        out.append(path)
    return out


def parse_baseline_manifest(text: str) -> set[str]:
    """Разобрать манифест baseline: имена файлов схемы, которые снимок содержит.

    Формат намеренно простой — по имени файла в строке, `#` начинает комментарий:
    манифест правит человек рядом с релизом, и любой формат сложнее ломался бы
    молча. Пустые строки и пути игнорируются: в журнал миграций попадает только
    базовое имя, как и у обычных файлов схемы.
    """
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        names.add(os.path.basename(line))
    return names


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements on top-level ";".

    Must not split inside: dollar-quoted blocks ($$...$$ / $tag$...$tag$),
    single-quoted string literals ('...', with '' as an escaped quote),
    double-quoted identifiers ("...", with "" as an escaped quote), "--"
    line comments, or "/* ... */" block comments — a literal ";" in any of
    these (e.g. a trailing comment like "-- null if not found; see below")
    would otherwise cut a real CREATE TABLE statement in half, corrupting it
    and every statement after it in the file.
    """
    stmts: list[str] = []
    buf: list[str] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        # Dollar-quoted block: $$ ... $$ or $tag$ ... $tag$
        if ch == "$":
            j = sql.find("$", i + 1)
            if j != -1:
                tag = sql[i:j + 1]
                closing = sql.find(tag, j + 1)
                if closing != -1:
                    end = closing + len(tag)
                    buf.append(sql[i:end])
                    i = end
                    continue
        # "--" line comment: consume through end of line
        if ch == "-" and sql[i + 1:i + 2] == "-":
            end = sql.find("\n", i + 2)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        # "/* ... */" block comment
        if ch == "/" and sql[i + 1:i + 2] == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue
        # '...' string literal, '' is an escaped quote
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j + 1:j + 2] != "'":
                    break
                j = j + 2 if sql[j] == "'" else j + 1
            end = min(j + 1, n)
            buf.append(sql[i:end])
            i = end
            continue
        # "..." quoted identifier, "" is an escaped quote
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"' and sql[j + 1:j + 2] != '"':
                    break
                j = j + 2 if sql[j] == '"' else j + 1
            end = min(j + 1, n)
            buf.append(sql[i:end])
            i = end
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt and _has_sql_content(stmt):
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    last = "".join(buf).strip()
    if last and _has_sql_content(last):
        stmts.append(last)
    return stmts


def _has_sql_content(stmt: str) -> bool:
    """True if `stmt` has any executable content left after stripping comments.

    A trailing "-- comment" at the end of a schema file (with no statement
    after it) survives splitting as its own "statement" that's pure comment —
    sending that to asyncpg's execute() raises a confusing internal
    AttributeError (no actual command for the server to acknowledge), so
    these must be filtered out before we ever call conn.execute().
    """
    stripped = re.sub(r"--[^\n]*", "", stmt)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    return bool(stripped.strip())


class _MigrationFileFailed(Exception):
    """Оператор миграции упал — сигнал откатить ВЕСЬ файл (транзакция на файл)."""


async def apply_migration_statements(conn, statements, label: str = "?"):
    """Применить операторы ОДНОГО файла миграции: целиком или никак.

    Возвращает (ok, error_count, last_error).

    Транзакция на файл: сбой на пятом операторе из десяти откатывает и первые
    четыре — схема не остаётся полуприменённой. Savepoint на КАЖДЫЙ оператор
    обязателен: в Postgres упавший оператор переводит транзакцию в aborted, и без
    savepoint безобидное «already exists» убивало бы весь остаток файла, то есть
    идемпотентный повтор миграций перестал бы работать.

    Отдельная функция, а не код внутри create_pool, чтобы тест на живой БД звал
    ИМЕННО ЭТО, а не свою копию логики.
    """
    err_count = 0
    last_error = None
    try:
        async with conn.transaction():
            for stmt in statements:
                try:
                    async with conn.transaction():          # savepoint
                        await conn.execute(stmt)
                except Exception as exc:
                    err = str(exc)
                    # Идемпотентный повтор — не ошибка: savepoint откатан, идём дальше.
                    if any(k in err.lower() for k in ("already exists", "duplicate key")):
                        continue
                    log.warning("Schema %s statement failed: %s | stmt: %.200s",
                                label, exc, stmt)
                    err_count += 1
                    last_error = err[:500]
                    raise _MigrationFileFailed(err)
    except _MigrationFileFailed:
        return False, err_count, last_error
    return True, 0, None


class SchemaMigrationError(RuntimeError):
    """Критичная миграция не применилась — стартовать на такой схеме нельзя."""


# Сколько раз повторить файл, который не смог взять блокировку. Пауза растёт
# (2с, 4с, 6с) — за это время старый контейнер деплоя обычно уже завершается.
_LOCK_RETRIES = 3


def _is_lock_timeout(err: str | None) -> bool:
    """Ошибка «не дождались блокировки», а не ошибка самой схемы.

    Такую миграцию имеет смысл повторить: SQL корректен, просто таблица была
    занята. Ошибку синтаксиса или несуществующую колонку повторять бессмысленно.
    """
    if not err:
        return False
    low = err.lower()
    return "lock timeout" in low or "canceling statement due to lock timeout" in low


# После миграций этих таблиц обязано существовать: без них платформа не работает
# (очередь операций, аудит, журнал), и «тихий» старт означает молча теряемые
# события вместо честного падения.
_CRITICAL_TABLES = ("activity_log", "operation_audit", "operation_queue")


async def create_pool() -> asyncpg.Pool:
    """Create asyncpg connection pool and apply all schema migrations.

    Searches for schema*.sql files in project root and database/ subdirectory,
    applies them in version order, and tracks migration status.

    Returns:
        Configured asyncpg connection pool.

    Raises:
        Exception: If pool creation or critical migration fails.
    """
    pool_config = get_pool_config()
    log_pool_config(pool_config)
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        **pool_config,
    )
    async with pool.acquire() as conn:
        # ── ПОТОЛОК ОЖИДАНИЯ БЛОКИРОВКИ ──────────────────────────────────────
        # Без него DDL миграции ждёт блокировку БЕСКОНЕЧНО, и это кладёт продукт
        # целиком. Сценарий обычного деплоя: новый контейнер накатывает
        # `ALTER TABLE tg_accounts ...`, а старый ещё жив и держит на этой
        # таблице читающую транзакцию. ALTER встаёт в очередь за ACCESS
        # EXCLUSIVE — и, что важнее, ВСЕ последующие читатели встают за ALTER.
        # Итог: страница мини-аппа отдаётся (статика), а каждый запрос к данным
        # висит до таймаута шлюза. Снаружи — «приложение не загружается».
        #
        # С lock_timeout DDL, не получивший блокировку за отведённое время,
        # честно падает: файл откатывается, помечается неуспешным и повторяется
        # ниже, а приложение ВСЁ РАВНО СТАРТУЕТ. Пропущенная миграция хуже
        # аварии, но лучше замороженной базы, и она видна в schema_migrations.
        _lock_timeout = (os.getenv("SCHEMA_LOCK_TIMEOUT") or "5s").strip()
        if not re.fullmatch(r"\d{1,6}(ms|s|min)?", _lock_timeout):
            log.warning("SCHEMA_LOCK_TIMEOUT=%r не похоже на интервал — беру 5s",
                        _lock_timeout)
            _lock_timeout = "5s"
        try:
            await conn.execute(f"SET lock_timeout = '{_lock_timeout}'")
        except Exception as _lt_exc:
            log.warning("не удалось задать lock_timeout: %s", _lt_exc)

        # Run all schema migration files in order — search both root and database/ subdir
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_dir = os.path.join(base_dir, "database")
        all_paths = glob.glob(os.path.join(base_dir, "schema*.sql")) + glob.glob(
            os.path.join(db_dir, "schema*.sql")
        )

        # Порядок применения и дедуп — в ordered_migration_files, чтобы он был
        # один и проверяемый тестом, а не переписан здесь по месту.
        schema_files = ordered_migration_files(all_paths)

        # Таблица учёта миграций — наблюдаемость (какие файлы применились чисто,
        # какие с ошибками). Не влияет на поведение, только фиксирует статус.
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename    TEXT PRIMARY KEY,
                       status      TEXT NOT NULL,        -- 'ok' | 'warnings'
                       error_count INT  NOT NULL DEFAULT 0,
                       last_error  TEXT,
                       applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )
        except Exception as _mig_exc:
            log.warning("schema_migrations table create failed: %s", _mig_exc)

        # Уже применённые ЧИСТО файлы пропускаем — иначе на каждом старте
        # перезапускались ВСЕ ~154 schema-файла целиком (тысячи no-op запросов +
        # риск, что медленный CREATE INDEX из нового файла залочит старт и Railway
        # убьёт деплой по health-check — «Application failed to respond»). Файлы со
        # статусом 'warnings' и незаписанные — перезапускаем (ретрай/новые).
        _applied_ok: set[str] = set()
        try:
            _rows = await conn.fetch("SELECT filename FROM schema_migrations WHERE status='ok'")
            _applied_ok = {r["filename"] for r in _rows}
        except Exception:
            _applied_ok = set()

        # ── Baseline: пустая база не должна проигрывать 190+ файлов ──────────
        # Журнал выше избавил ОБНОВЛЕНИЕ от повторного проигрывания, но ПЕРВЫЙ
        # запуск на чистой базе по-прежнему прокатывает всю историю миграций
        # подряд: чем дальше, тем дольше старт нового окружения и тем выше шанс,
        # что развалится давняя миграция, чей контекст уже никто не помнит.
        # Baseline — снимок схемы на некоторой версии плюс манифест файлов,
        # которые он уже содержит. Берём его ТОЛЬКО на заведомо чистой базе
        # (пустой журнал И нет ключевой таблицы): у существующего окружения
        # снимок затирал бы данные, а у наполовину мигрированного — молча
        # пометил бы непроигранные файлы применёнными.
        # Сам файл снимка генерируется из живой базы: deploy/scripts/make_schema_baseline.py.
        _baseline_sql = os.path.join(base_dir, "schema_baseline.sql")
        _baseline_manifest = os.path.join(base_dir, "schema_baseline.manifest")
        _baselined: set[str] = set()
        if not _applied_ok and os.path.exists(_baseline_sql) and os.path.exists(_baseline_manifest):
            try:
                _core_exists = await conn.fetchval("SELECT to_regclass('public.tg_accounts')")
            except Exception:
                _core_exists = "unknown"      # не смогли проверить — не рискуем
            if _core_exists is None:
                with open(_baseline_manifest, encoding="utf-8") as _mf:
                    _baselined = parse_baseline_manifest(_mf.read())
                with open(_baseline_sql, encoding="utf-8") as _bf:
                    _b_stmts = _split_sql_statements(_bf.read())
                _b_ok, _b_errs, _b_last = await apply_migration_statements(
                    conn, _b_stmts, "schema_baseline.sql")
                if _b_ok:
                    log.info("Schema baseline применён: %d операторов, %d файлов "
                             "истории помечены применёнными",
                             len(_b_stmts), len(_baselined))
                    for _bn_b in sorted(_baselined) + ["schema_baseline.sql"]:
                        try:
                            await conn.execute(
                                """INSERT INTO schema_migrations(filename, status, error_count, applied_at)
                                   VALUES($1, 'ok', 0, now())
                                   ON CONFLICT (filename) DO NOTHING""", _bn_b)
                        except Exception:
                            pass
                    _applied_ok = set(_baselined)
                else:
                    # Снимок не лёг — откатываться некуда и незачем: дальше
                    # отработает обычный путь по файлам, он идемпотентен.
                    log.error("Schema baseline не применился (%d ошибок, последняя: %s) — "
                              "иду обычным путём по файлам истории", _b_errs, _b_last)
                    _baselined = set()
            else:
                log.info("Schema baseline пропущен: база не пустая "
                         "(есть tg_accounts) — играю историю миграций как обычно")

        applied = 0
        failed = 0
        skipped = 0
        failed_files: list[str] = []
        for path in schema_files:
            _bn0 = os.path.basename(path)
            if _bn0 in _applied_ok:
                skipped += 1
                continue
            with open(path, encoding="utf-8") as f:
                sql = f.read().strip()
            if not sql:
                continue
            # Split into individual top-level statements. We split on ";"
            # only OUTSIDE dollar-quoted blocks (DO $$ ... $$ blocks contain
            # their own semicolons that must not be split).
            statements = _split_sql_statements(sql)
            _bn = os.path.basename(path)
            # Транзакция на файл + savepoint на оператор — см. докстринг функции.
            file_ok, _err_count, _last_error = await apply_migration_statements(
                conn, statements, _bn)
            # Не получили блокировку — это НЕ ошибка схемы, а занятая таблица
            # (обычно старый контейнер, который вот-вот умрёт). Повторяем с
            # паузой: за несколько секунд деплой почти всегда доигрывает.
            _tries = 0
            while (not file_ok and _tries < _LOCK_RETRIES
                   and _is_lock_timeout(_last_error)):
                _tries += 1
                _pause = 2.0 * _tries
                log.warning(
                    "Schema %s: таблица занята другим соединением — повтор %d/%d "
                    "через %.0fс (приложение стартует в любом случае)",
                    _bn, _tries, _LOCK_RETRIES, _pause)
                await asyncio.sleep(_pause)
                file_ok, _err_count, _last_error = await apply_migration_statements(
                    conn, statements, _bn)
            if file_ok:
                applied += 1
            else:
                failed += 1
                failed_files.append(_bn)
            # Фиксируем статус файла (best-effort)
            try:
                await conn.execute(
                    """INSERT INTO schema_migrations(filename, status, error_count, last_error, applied_at)
                       VALUES($1, $2, $3, $4, now())
                       ON CONFLICT (filename) DO UPDATE
                       SET status=EXCLUDED.status, error_count=EXCLUDED.error_count,
                           last_error=EXCLUDED.last_error, applied_at=now()""",
                    _bn, "ok" if file_ok else "warnings", _err_count, _last_error,
                )
            except Exception:
                pass

        if failed:
            # Раньше это был log.info и терялось — теперь ERROR со списком файлов.
            log.error(
                "Schema migration: %d files OK, %d WITH ERRORS: %s "
                "(детали в таблице schema_migrations)",
                applied, failed, ", ".join(failed_files[:30]),
            )
        else:
            log.info("Schema migration: %d files OK, %d skipped (already applied), 0 with errors",
                     applied, skipped)

        # Критичные таблицы: раньше их отсутствие только ЛОГИРОВАЛОСЬ, и процесс
        # спокойно стартовал на нерабочей схеме — операции и аудит молча
        # проваливались, а причина тонула в логе. Теперь падаем на старте:
        # супервизор перезапустит и ошибка будет видна сразу, а не через сутки
        # по жалобе «операции ничего не делают».
        _missing = []
        for tbl in _CRITICAL_TABLES:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=$1)",
                tbl,
            )
            if not exists:
                _missing.append(tbl)
        _strict = os.getenv("SCHEMA_STRICT", "").strip().lower() in ("1", "true", "yes")
        if _missing:
            log.error(
                "CRITICAL: после миграций нет таблиц %s — операции и аудит будут "
                "молча проваливаться. Примените соответствующие schema_vN.sql.",
                _missing)
            # Падать по умолчанию НЕЛЬЗЯ: это загоняет процесс в цикл
            # перезапусков, и вместо деградации получается полная недоступность.
            # Ошибка аудита-фикса была именно здесь. Роняем только по явному
            # SCHEMA_STRICT, когда команда осознанно этого хочет.
            if _strict:
                raise SchemaMigrationError(
                    f"критичные таблицы отсутствуют после миграций: {', '.join(_missing)}")

        # Строгий режим: любой файл с ошибкой роняет старт. По умолчанию
        # ВЫКЛЮЧЕН — в базе 180 файлов, и падение по умолчанию заблокировало бы
        # деплой из-за одного проблемного легаси-файла.
        if failed and _strict:
            raise SchemaMigrationError(
                f"SCHEMA_STRICT: миграции с ошибками: {', '.join(failed_files[:30])}")

        await verify_vault_key(conn, strict=_strict)
    return pool


# Канарейка хранилища: строка, зашифрованная тем же ключом, что и секреты.
_VAULT_CANARY_PLAIN = "infragram-vault-canary-v1"


async def verify_vault_key(conn, *, strict: bool = False) -> str:
    """Проверить на старте, что ключ шифрования тот же, что и раньше.

    Все секреты — строки сессий, прокси, токены ботов, ключи SMM-панелей —
    лежат зашифрованными одним ключом. Если ключ сменился, расшифровка падает
    не на старте, а при первом обращении к каждой строке: аккаунты по одному
    перестают подключаться, прокси «не работают», и в логе видно только
    троттленное предупреждение из token_vault. Весь флот тихо умирает, а
    причина выглядит как отказ Telegram.

    Сменить ключ незаметно особенно легко, когда TOKEN_ENCRYPTION_KEY не задан:
    тогда ключ выводится из токена бота, и обычная ротация токена бота —
    операция, к шифрованию отношения не имеющая, — обесценивает всё хранилище.

    Поэтому при первом старте рядом с секретами сохраняется канарейка,
    зашифрованная тем же ключом, а каждый следующий старт её расшифровывает.
    Возвращает 'ok', 'initialized', 'changed' или 'skipped'.
    """
    from services.token_vault import decrypt_token, encrypt_token

    # Таблицу заводит schema_v219_vault_key_check.sql. Здесь её не создаём:
    # таблица, появляющаяся при первом обращении, не оставляет следа в схеме —
    # перестали звать создателя, и раздел молча исчез.
    try:
        row = await conn.fetchrow("SELECT canary FROM vault_key_check WHERE id=1")
    except Exception:
        # Таблицы нет или база недоступна — это не повод не стартовать.
        log.warning("vault_key_check: проверка ключа шифрования не выполнена", exc_info=True)
        return "skipped"

    # Дальше работает шифрование. Самопроверка старта не имеет права ронять
    # процесс собственной поломкой — отсутствием библиотеки, отказом записи, —
    # поэтому всё, что не является самим несовпадением ключа, лишь помечается.
    try:
        if row is None:
            await conn.execute(
                "INSERT INTO vault_key_check(id, canary) VALUES(1, $1) "
                "ON CONFLICT (id) DO NOTHING",
                encrypt_token(_VAULT_CANARY_PLAIN),
            )
            log.info("vault_key_check: канарейка ключа шифрования сохранена")
            return "initialized"
        matches = decrypt_token(row["canary"]) == _VAULT_CANARY_PLAIN
    except Exception:
        log.warning("vault_key_check: проверка ключа шифрования не выполнена", exc_info=True)
        return "skipped"

    if matches:
        return "ok"

    log.error(
        "КЛЮЧ ШИФРОВАНИЯ ИЗМЕНИЛСЯ. Всё, что было зашифровано прежним ключом, "
        "сейчас нечитаемо: строки сессий (tg_accounts, booster_sessions), "
        "прокси (user_proxies), токены ботов (bot_warehouse), ключи SMM-панелей "
        "(smm_panels). Аккаунты будут по одному переставать подключаться, и "
        "выглядеть это будет как отказ Telegram. Верните прежний "
        "TOKEN_ENCRYPTION_KEY — данные при этом не теряются, они просто "
        "зашифрованы старым ключом. Если ключ сменён намеренно, перешифруйте "
        "секреты старым ключом на новый и обновите строку vault_key_check. "
        "Частая причина: TOKEN_ENCRYPTION_KEY не задан, ключ выводится из "
        "токена бота, и токен бота ротировали."
    )
    if strict:
        raise SchemaMigrationError(
            "ключ шифрования не совпадает с тем, которым зашифрованы секреты")
    return "changed"


# ── Performance Helpers ────────────────────────────────────────────────────


async def run_concurrent_queries(pool: asyncpg.Pool, queries: list[tuple[str, tuple]]) -> list:
    """Execute multiple independent queries concurrently using a single connection.

    Each query is (sql, params) tuple. Returns list of results.
    More efficient than multiple pool.acquire() calls.
    If a query fails, returns empty list for that position.
    """
    async with pool.acquire() as conn:
        async def _safe_fetch(sql, params):
            try:
                return await conn.fetch(sql, *params)
            except Exception:
                return []
        return await asyncio.gather(
            *(_safe_fetch(sql, params) for sql, params in queries)
        )


async def batch_upsert_users(pool: asyncpg.Pool, bot_id: int, users: list[dict]) -> int:
    """Batch upsert users using executemany for better performance.

    Returns count of new rows inserted.
    """
    if not users:
        return 0

    rows = [
        (
            bot_id,
            u["user_id"],
            u.get("username"),
            u.get("first_name"),
            u.get("last_name"),
            u.get("language_code"),
            u.get("phone"),
        )
        for u in users
    ]

    async with pool.acquire() as conn:
        result = await conn.executemany(
            """INSERT INTO bot_users (bot_id, user_id, username, first_name, last_name, language_code, phone)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (bot_id, user_id) DO UPDATE SET
                   last_seen     = NOW(),
                   username      = EXCLUDED.username,
                   first_name    = EXCLUDED.first_name,
                   last_name     = EXCLUDED.last_name,
                   language_code = EXCLUDED.language_code,
                   phone         = COALESCE(EXCLUDED.phone, bot_users.phone)""",
            rows,
        )
    return len(rows)


async def batch_insert(
    pool: asyncpg.Pool,
    table: str,
    rows: list[dict],
    *,
    on_conflict: str | None = None,
    chunk_size: int = 500,
) -> int:
    """Generic batch insert using executemany with chunking.

    Args:
        pool: Database connection pool.
        table: Target table name.
        rows: List of dicts where keys are column names.
        on_conflict: Optional ON CONFLICT clause (e.g. "DO NOTHING", "DO UPDATE SET ...").
        chunk_size: Max rows per executemany call.

    Returns:
        Total number of rows inserted.
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    col_names = ", ".join(columns)
    conflict = f" ON CONFLICT {on_conflict}" if on_conflict else ""
    sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}){conflict}"

    total = 0
    async with pool.acquire() as conn:
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            tuples = [tuple(r.get(c) for c in columns) for r in chunk]
            await conn.executemany(sql, tuples)
            total += len(tuples)
    return total


async def batch_update(
    pool: asyncpg.Pool,
    table: str,
    updates: list[dict],
    *,
    key_column: str = "id",
    chunk_size: int = 500,
) -> int:
    """Generic batch update using executemany with chunking.

    Each dict in updates must contain key_column plus the columns to update.

    Args:
        pool: Database connection pool.
        table: Target table name.
        updates: List of dicts with key_column and columns to update.
        key_column: Column used to identify rows (default: 'id').
        chunk_size: Max rows per executemany call.

    Returns:
        Total number of rows updated.
    """
    if not updates:
        return 0

    other_cols = [c for c in updates[0] if c != key_column]
    set_clause = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(other_cols))
    sql = f"UPDATE {table} SET {set_clause} WHERE {key_column}=$1"

    total = 0
    async with pool.acquire() as conn:
        for i in range(0, len(updates), chunk_size):
            chunk = updates[i : i + chunk_size]
            tuples = [
                (r[key_column],) + tuple(r[c] for c in other_cols)
                for r in chunk
            ]
            await conn.executemany(sql, tuples)
            total += len(tuples)
    return total


async def get_query_stats(pool: asyncpg.Pool) -> dict:
    """Get database connection pool and query statistics.

    Returns pool utilization, pg_stat_statements top queries (if extension is loaded),
    and general performance metrics.

    Returns:
        Dict with pool stats and top queries.
    """
    stats: dict = {
        "pool": {
            "size": pool.get_size(),
            "idle": pool.get_idle_size(),
            "active": pool.get_size() - pool.get_idle_size(),
            "utilization": round(
                (pool.get_size() - pool.get_idle_size())
                / max(pool.get_size(), 1)
                * 100,
                1,
            ),
        },
        "top_queries": [],
    }

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT
                       query,
                       calls,
                       total_exec_time::bigint AS total_ms,
                       mean_exec_time::bigint AS mean_ms,
                       rows::bigint
                   FROM pg_stat_statements
                   ORDER BY total_exec_time DESC
                   LIMIT 10"""
            )
            stats["top_queries"] = [dict(r) for r in rows]
    except Exception:
        pass

    return stats


# ── Managed bots ───────────────────────────────────────────────────────────


def _dec_bot_rows(rows: list[asyncpg.Record]) -> list[dict]:
    """Decrypt token field in managed_bots query results.

    Args:
        rows: List of database records from managed_bots table.

    Returns:
        List of dicts with decrypted token field.
    """
    from services.token_vault import decrypt_token as _dt
    out = []
    for r in rows:
        d = dict(r)
        if d.get("token"):
            d["token"] = _dt(d["token"])
        out.append(d)
    return out


async def fetchrow_bot(pool: asyncpg.Pool, query: str, *args: object) -> dict | None:
    """Fetch single managed_bot row with auto-decrypted token.

    Args:
        pool: Database connection pool.
        query: SQL query to execute.
        *args: Query parameters.

    Returns:
        Dict with bot data and decrypted token, or None if not found.
    """
    row = await pool.fetchrow(query, *args)
    if not row:
        return None
    d = dict(row)
    if d.get("token"):
        from services.token_vault import decrypt_token as _dt
        d["token"] = _dt(d["token"])
    return d


async def fetch_bots(pool: asyncpg.Pool, query: str, *args: object) -> list[dict]:
    """Fetch multiple managed_bot rows with auto-decrypted tokens.

    Args:
        pool: Database connection pool.
        query: SQL query to execute.
        *args: Query parameters.

    Returns:
        List of dicts with bot data and decrypted tokens.
    """
    rows = await pool.fetch(query, *args)
    return _dec_bot_rows(rows)


async def save_chatlist_folder(
    pool: asyncpg.Pool, owner_id: int, record: dict, acc_id: int | None = None,
) -> int:
    """Сохранить черновик общей папки (до экспорта ссылки). Возвращает id."""
    row = await pool.fetchrow(
        """INSERT INTO chatlist_folders
               (owner_id, acc_id, title, chat_ids, chat_count, instance_id, status)
           VALUES ($1,$2,$3,$4,$5,$6,'draft')
        RETURNING id""",
        owner_id, acc_id, record["title"],
        [int(c) for c in record["chat_ids"]], int(record["chat_count"]),
        record.get("instance_id"))
    return int(row["id"])


async def set_chatlist_folder_result(
    pool: asyncpg.Pool, folder_id: int, owner_id: int, result: dict,
) -> None:
    """Записать итог экспорта ссылки (owner-scoped)."""
    if result.get("ok"):
        await pool.execute(
            """UPDATE chatlist_folders
                  SET status='ready', invite_link=$3, invite_slug=$4,
                      filter_id=$5, error=NULL, updated_at=now()
                WHERE id=$1 AND owner_id=$2""",
            folder_id, owner_id, result.get("invite_link"),
            result.get("slug"), result.get("filter_id"))
    else:
        await pool.execute(
            """UPDATE chatlist_folders
                  SET status='failed', error=$3, updated_at=now()
                WHERE id=$1 AND owner_id=$2""",
            folder_id, owner_id, str(result.get("error") or "ошибка")[:300])


async def replace_bot_token(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    token: str,
    username: str = "",
    first_name: str = "",
) -> bool:
    """Заменить токен бота НА МЕСТЕ, сохранив всё остальное.

    Разрыв: заменить токен было нельзя. `add_bot` при существующем bot_id
    возвращает «уже добавлен» и токен НЕ обновляет, поэтому у бота с отозванным
    токеном оставался единственный путь — удалить и добавить заново. А
    `bot_users` висит на `managed_bots` с `ON DELETE CASCADE`: вместе с ботом
    стиралась вся его аудитория. Продукт предлагал вылечить молчание бота ценой
    базы подписчиков.

    Здесь bot_id не меняется, поэтому подписчики, воронки, правила, расписания
    и статистика остаются на месте. Возвращает True, если строка обновлена.
    """
    from services.token_vault import encrypt_token as _enc_tok

    row = await pool.fetchrow(
        """UPDATE managed_bots
              SET token=$3,
                  username=COALESCE(NULLIF($4,''), username),
                  first_name=COALESCE(NULLIF($5,''), first_name),
                  is_active=TRUE,
                  fail_streak=0, last_error=NULL, dead_notified_at=NULL
            WHERE bot_id=$1 AND added_by=$2
        RETURNING bot_id""",
        bot_id, owner_id, _enc_tok(token), username or "", first_name or "")
    return row is not None


async def add_bot(
    pool: asyncpg.Pool,
    token: str,
    bot_id: int,
    username: str,
    first_name: str,
    added_by: int,
    bot: object = None,
) -> bool | str:
    """Add a new bot to the managed_bots table.

    Args:
        pool: Database connection pool.
        token: Bot token from BotFather.
        bot_id: Bot numeric ID.
        username: Bot username.
        first_name: Bot display name.
        added_by: User ID of the owner.
        bot: Optional bot instance for referral rewards.

    Returns:
        True if inserted, False if already owned by this user,
        'taken' if owned by another user.
    """
    from services.token_vault import encrypt_token as _enc_tok

    existing = await pool.fetchrow(
        "SELECT added_by, is_active FROM managed_bots WHERE bot_id=$1", bot_id
    )
    if existing is not None:
        if existing["added_by"] == added_by:
            if existing["is_active"]:
                return False  # уже добавлен и работает
            # Своя выключенная строка: бот был убран из списка или отключён
            # после мёртвого токена. Раньше здесь тоже отвечали «уже добавлен»,
            # и бот было не вернуть в принципе — тупик на ровном месте.
            # Поднимаем ту же строку: аудитория, воронки и история на месте.
            await pool.execute(
                """UPDATE managed_bots
                      SET token=$3,
                          username=COALESCE(NULLIF($4,''), username),
                          first_name=COALESCE(NULLIF($5,''), first_name),
                          is_active=TRUE,
                          fail_streak=0, last_error=NULL, dead_notified_at=NULL
                    WHERE bot_id=$1 AND added_by=$2""",
                bot_id, added_by, _enc_tok(token), username or "", first_name or "")
            return True
        if existing["is_active"]:
            return "taken"
        # Прежний владелец бота у себя убрал, а токен есть у нового. Строку
        # отдаём новому владельцу ЧИСТОЙ: аудитория и история прежнего к боту
        # больше не относятся, и достаться новому хозяину не должны. Здесь
        # каскад срабатывает намеренно — это единственное место, где данные
        # действительно стираются.
        await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot_id)

    try:
        await pool.execute(
            """INSERT INTO managed_bots (token, bot_id, username, first_name, added_by)
               VALUES ($1, $2, $3, $4, $5)""",
            _enc_tok(token),
            bot_id,
            username,
            first_name,
            added_by,
        )
    except asyncpg.UniqueViolationError:
        owner = await pool.fetchval(
            "SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id
        )
        return False if owner == added_by else "taken"
    # Referral activation: first bot creation counts as "active"
    existing_bots = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
            added_by,
        )
        or 0
    )
    if existing_bots == 1:  # this was the first bot
        referrer_id = await mark_referral_activated(pool, added_by)
        if referrer_id and bot is not None:
            # Check and grant tier rewards for the referrer
            try:
                await check_and_grant_rewards(pool, referrer_id, bot)
            except Exception:
                log_exc_swallow(log, "add_bot: check_and_grant_rewards failed")
    return True


async def get_bots(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT m.*,
                  COALESCE(aud.cnt, 0) AS audience_count,
                  COALESCE(ar.ar_cnt, 0) AS active_replies_count
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt
               FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id = m.bot_id
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS ar_cnt
               FROM auto_replies WHERE is_active=TRUE GROUP BY bot_id
           ) ar ON ar.bot_id = m.bot_id
           -- Скобки обязательны. AND связывает крепче OR, поэтому без них
           -- условие читалось как «свои ИЛИ экосистемные ИЛИ (по workspace И
           -- активные)»: на СВОИ боты фильтр is_active не действовал вовсе.
           -- Из-за этого удалённый в Mini App бот (там удаление — это
           -- is_active=FALSE) продолжал висеть в списке бота, и «пауза» тоже
           -- ничего не меняла.
           WHERE (m.added_by=$1
              OR m.bot_id IN (
                  SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                  JOIN ecosystems e ON e.id=eb.ecosystem_id
                  WHERE e.owner_id=$1
                     OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
              )
              OR m.bot_id IN (
                  SELECT DISTINCT b.bot_id FROM managed_bots b
                  JOIN workspaces w ON w.owner_id=b.added_by
                  JOIN workspace_members wm ON wm.workspace_id=w.id
                  WHERE wm.user_id=$1
              ))
           AND m.is_active=TRUE
           ORDER BY m.added_at DESC""",
        added_by,
    )
    return _dec_bot_rows(rows)


async def get_bot(
    pool: asyncpg.Pool, bot_id: int, added_by: int
) -> dict | None:
    row = await pool.fetchrow(
        """SELECT * FROM managed_bots
           WHERE bot_id=$1 AND is_active=TRUE
              AND (added_by=$2
                   OR bot_id IN (
                       SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                       JOIN ecosystems e ON e.id=eb.ecosystem_id
                       WHERE e.owner_id=$2
                          OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$2)
                   )
                   OR bot_id IN (
                       SELECT DISTINCT b.bot_id FROM managed_bots b
                       JOIN workspaces w ON w.owner_id=b.added_by
                       JOIN workspace_members wm ON wm.workspace_id=w.id
                       WHERE wm.user_id=$2
                   ))""",
        bot_id,
        added_by,
    )
    if row is None:
        return None
    return _dec_bot_rows([row])[0]


async def delete_bot(pool: asyncpg.Pool, bot_id: int, added_by: int) -> bool:
    """Убрать бота у владельца. Строка остаётся, аудитория не теряется.

    Раньше это был DELETE, а на managed_bots(bot_id) висит десяток таблиц с
    ON DELETE CASCADE: bot_users, funnels, auto_replies, broadcasts, статистика.
    То есть «убрать бота из списка» стирало всю его базу подписчиков и историю
    — безвозвратно, потому что удалённую строку неоткуда взять. Продукт уже
    признал эту цену слишком высокой: ради неё появилась replace_bot_token, а
    Mini App удаляет бота именно снятием is_active.

    Теперь обе двери ведут в одно место: бот пропадает из списков (все они
    фильтруют is_active), а данные остаются. Вернуть бота — добавить его
    заново тем же токеном, add_bot поднимет ту же строку.

    Данные стираются только тогда, когда бота действительно забирает ДРУГОЙ
    владелец: это делает add_bot, и там иначе нельзя — чужая аудитория не
    должна достаться новому хозяину.
    """
    result = await pool.execute(
        "UPDATE managed_bots SET is_active=FALSE WHERE bot_id=$1 AND added_by=$2",
        bot_id,
        added_by,
    )
    return result == "UPDATE 1"


async def save_bot_note(
    pool: asyncpg.Pool, bot_id: int, added_by: int, note: str
) -> None:
    await pool.execute(
        "UPDATE managed_bots SET note=$3 WHERE bot_id=$1 AND added_by=$2",
        bot_id,
        added_by,
        note,
    )


# ── Audience ───────────────────────────────────────────────────────────────


async def upsert_users(pool: asyncpg.Pool, bot_id: int, users: list[dict]) -> int:
    """Insert or refresh last_seen for each user. Returns count of new rows."""
    if not users:
        return 0
    with timed(log, "upsert_users", extra={"bot_id": bot_id, "count": len(users)}):
        return await batch_upsert_users(pool, bot_id, users)


async def safe_count(pool: asyncpg.Pool, query: str, *args: object) -> int:
    """COUNT-запрос, устойчивый к миграционному лагу.

    Если таблица/колонка ещё не создана (UndefinedColumn при отставании
    схемы) — возвращает 0 вместо падения, а не даёт всплыть исключению в
    вызывающую логику рассылок. Логирует причину, секреты в query не попадают.
    """
    try:
        return int(await pool.fetchval(query, *args) or 0)
    except Exception as e:  # noqa: BLE001 — намеренно широко: любой сбой БД → 0
        log.warning("safe_count error: %s | query=%.120s", e, query)
        return 0


async def get_audience_count(pool: asyncpg.Pool, bot_id: int) -> int:
    # suspect=FALSE — накрученных (флаг ставит flood_guard, только если защита
    # включена) не считаем аудиторией; для ботов без защиты флаг всегда FALSE.
    return await safe_count(
        pool,
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE "
        "AND suspect=FALSE",
        bot_id,
    )


async def get_audience_user_ids(pool: asyncpg.Pool, bot_id: int) -> list[int]:
    rows = await pool.fetch(
        "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE "
        "AND is_blocked=FALSE AND suspect=FALSE",
        bot_id,
    )
    return [r["user_id"] for r in rows]


async def compare_audiences(pool: asyncpg.Pool, bot_id_a: int, bot_id_b: int) -> dict:
    rows = await pool.fetch(
        """SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE
           INTERSECT
           SELECT user_id FROM bot_users WHERE bot_id=$2 AND is_active=TRUE""",
        bot_id_a,
        bot_id_b,
    )
    count_a = await get_audience_count(pool, bot_id_a)
    count_b = await get_audience_count(pool, bot_id_b)
    overlap = len(rows)
    return {
        "count_a": count_a,
        "count_b": count_b,
        "overlap": overlap,
        "overlap_pct_a": round(overlap / count_a * 100, 1) if count_a else 0,
        "overlap_pct_b": round(overlap / count_b * 100, 1) if count_b else 0,
    }


async def get_user_by_id(
    pool: asyncpg.Pool, bot_id: int, user_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM bot_users WHERE bot_id=$1 AND user_id=$2", bot_id, user_id
    )


async def block_user(
    pool: asyncpg.Pool, bot_id: int, user_id: int, blocked: bool
) -> None:
    await pool.execute(
        "UPDATE bot_users SET is_blocked=$3 WHERE bot_id=$1 AND user_id=$2",
        bot_id,
        user_id,
        blocked,
    )


async def mark_user_inactive(pool: asyncpg.Pool, bot_id: int, user_id: int) -> None:
    await pool.execute(
        "UPDATE bot_users SET is_active=FALSE WHERE bot_id=$1 AND user_id=$2",
        bot_id,
        user_id,
    )


# ── Broadcasts ────────────────────────────────────────────────────────────


async def create_broadcast(
    pool: asyncpg.Pool,
    bot_id: int,
    message_text: str,
    total: int,
    created_by: int,
    photo_file_id: str | None = None,
    target_user_ids: list[int] | None = None,
    buttons: list[dict] | None = None,
    silent: bool = False,
) -> int:
    # target_user_ids сохраняем только для сегментных рассылок (подмножество
    # аудитории); NULL = полная аудитория бота. Используется resume после
    # рестарта, чтобы сегментная рассылка не ушла всей аудитории.
    # buttons/silent сохраняем, чтобы кнопки и тихий режим пережили рестарт.
    import json as _json

    if total < 0:
        raise ValueError("total must be non-negative")
    if not message_text or not message_text.strip():
        raise ValueError("message_text must not be empty")

    target_json = _json.dumps(target_user_ids) if target_user_ids else None
    buttons_json = _json.dumps(buttons) if buttons else None
    try:
        return await pool.fetchval(
            """INSERT INTO broadcasts (bot_id, message_text, total_users, status, created_by, photo_file_id, target_user_ids, buttons, silent)
               VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb, $7::jsonb, $8) RETURNING id""",
            bot_id, message_text, total, created_by, photo_file_id, target_json, buttons_json, bool(silent),
        )
    except asyncpg.UndefinedColumnError:
        try:
            return await pool.fetchval(
                """INSERT INTO broadcasts (bot_id, message_text, total_users, status, created_by, photo_file_id, target_user_ids, buttons)
                   VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb, $7::jsonb) RETURNING id""",
                bot_id, message_text, total, created_by, photo_file_id, target_json, buttons_json,
            )
        except asyncpg.UndefinedColumnError:
            # Совместимость со старой схемой без колонок buttons/silent
            return await pool.fetchval(
                """INSERT INTO broadcasts (bot_id, message_text, total_users, status, created_by, photo_file_id, target_user_ids)
                   VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb) RETURNING id""",
                bot_id, message_text, total, created_by, photo_file_id, target_json,
            )


async def update_broadcast(
    pool: asyncpg.Pool, broadcast_id: int, sent: int, failed: int, status: str
) -> None:
    await pool.execute(
        """UPDATE broadcasts
           SET sent_count=$2, failed_count=$3, status=$4,
               finished_at=CASE WHEN $4 IN ('done','cancelled','failed','partial') THEN NOW() ELSE NULL END
           WHERE id=$1""",
        broadcast_id,
        sent,
        failed,
        status,
    )


async def get_broadcast(
    pool: asyncpg.Pool, broadcast_id: int, bot_id: int | None = None
) -> asyncpg.Record | None:
    if bot_id is not None:
        return await pool.fetchrow(
            "SELECT * FROM broadcasts WHERE id=$1 AND bot_id=$2", broadcast_id, bot_id
        )
    return await pool.fetchrow("SELECT * FROM broadcasts WHERE id=$1", broadcast_id)


async def get_interrupted_broadcasts(pool: asyncpg.Pool) -> list[dict]:
    """Рассылки, оборвавшиеся на рестарте процесса (status running/pending).

    Возвращает dict с расшифрованным токеном бота и распарсенным target_user_ids
    (список или None). Используется resume при старте — broadcaster.run докатывает
    рассылку, пропуская уже доставленных через delivery log.
    """
    import json as _json
    try:
        rows = await pool.fetch(
            """SELECT b.id, b.bot_id, b.message_text, b.photo_file_id, b.target_user_ids,
                      b.buttons, b.silent, m.token
               FROM broadcasts b
               JOIN managed_bots m ON m.bot_id = b.bot_id
               WHERE b.status IN ('running', 'pending') AND m.is_active = TRUE
                 AND m.token IS NOT NULL
               ORDER BY b.id"""
        )
    except asyncpg.UndefinedColumnError:
        rows = await pool.fetch(
            """SELECT b.id, b.bot_id, b.message_text, b.photo_file_id, b.target_user_ids,
                      m.token
               FROM broadcasts b
               JOIN managed_bots m ON m.bot_id = b.bot_id
               WHERE b.status IN ('running', 'pending') AND m.is_active = TRUE
                 AND m.token IS NOT NULL
               ORDER BY b.id"""
        )
    from services.token_vault import decrypt_token as _dt
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if d.get("token"):
            d["token"] = _dt(d["token"])
        tgt = d.get("target_user_ids")
        if isinstance(tgt, str):
            try:
                d["target_user_ids"] = _json.loads(tgt)
            except Exception:
                d["target_user_ids"] = None
        btns = d.get("buttons")
        if isinstance(btns, str):
            try:
                d["buttons"] = _json.loads(btns)
            except Exception:
                d["buttons"] = None
        out.append(d)
    return out


async def log_broadcast_delivery(
    pool: asyncpg.Pool, broadcast_id: int, user_id: int
) -> None:
    """Record that a specific user received a broadcast (idempotent via ON CONFLICT DO NOTHING)."""
    await pool.execute(
        "INSERT INTO broadcast_delivery_log (broadcast_id, user_id) VALUES ($1, $2)"
        " ON CONFLICT DO NOTHING",
        broadcast_id,
        user_id,
    )


async def get_broadcast_delivered_ids(
    pool: asyncpg.Pool, broadcast_id: int
) -> set[int]:
    """Return the set of user_ids already delivered for a broadcast (for resume)."""
    rows = await pool.fetch(
        "SELECT user_id FROM broadcast_delivery_log WHERE broadcast_id=$1",
        broadcast_id,
    )
    return {r["user_id"] for r in rows}


async def get_recent_broadcasts(
    pool: asyncpg.Pool, bot_id: int, limit: int = 10
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT $2",
        bot_id,
        limit,
    )


async def get_broadcast_history(
    pool: asyncpg.Pool, bot_id: int, limit: int = 5
) -> list[asyncpg.Record]:
    """Return last N broadcasts with stats for summary view."""
    return await pool.fetch(
        "SELECT * FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT $2",
        bot_id,
        limit,
    )


# ── Audience stats ────────────────────────────────────────────────────────


async def get_audience_stats(pool: asyncpg.Pool, bot_id: int) -> dict:
    queries = [
        ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", (bot_id,)),
        ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=FALSE", (bot_id,)),
        ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '24 hours'", (bot_id,)),
        ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '7 days'", (bot_id,)),
        ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '30 days'", (bot_id,)),
        ("SELECT COALESCE(language_code, 'unknown') AS lang, COUNT(*) AS cnt FROM bot_users WHERE bot_id=$1 AND is_active=TRUE GROUP BY lang ORDER BY cnt DESC LIMIT 10", (bot_id,)),
    ]
    results = await run_concurrent_queries(pool, queries)
    total, inactive, joined_today, joined_week, joined_month, langs = results
    return {
        "total": (total[0]["count"] if total else 0) or 0,
        "inactive": (inactive[0]["count"] if inactive else 0) or 0,
        "joined_today": (joined_today[0]["count"] if joined_today else 0) or 0,
        "joined_week": (joined_week[0]["count"] if joined_week else 0) or 0,
        "joined_month": (joined_month[0]["count"] if joined_month else 0) or 0,
        "languages": [{"lang": r["lang"], "count": r["cnt"]} for r in langs],
    }


async def get_audience_full(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT user_id, username, first_name, last_name, language_code,
                  first_seen, last_seen, is_active
           FROM bot_users WHERE bot_id=$1 ORDER BY first_seen""",
        bot_id,
    )


# ── Message templates ─────────────────────────────────────────────────────


async def save_template(
    pool: asyncpg.Pool, owner_id: int, name: str, text: str
) -> bool:
    try:
        await pool.execute(
            "INSERT INTO message_templates (owner_id, name, text) VALUES ($1,$2,$3)",
            owner_id,
            name,
            text,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def get_templates(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM message_templates WHERE owner_id=$1 ORDER BY created_at DESC",
        owner_id,
    )


async def get_template(
    pool: asyncpg.Pool, template_id: int, owner_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM message_templates WHERE id=$1 AND owner_id=$2",
        template_id,
        owner_id,
    )


async def delete_template(pool: asyncpg.Pool, template_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM message_templates WHERE id=$1 AND owner_id=$2",
        template_id,
        owner_id,
    )
    return result == "DELETE 1"


async def update_template(
    pool: asyncpg.Pool, template_id: int, owner_id: int, name: str, text: str
) -> bool:
    """Update existing template name and text. Returns True on success."""
    try:
        result = await pool.execute(
            "UPDATE message_templates SET name=$1, text=$2 WHERE id=$3 AND owner_id=$4",
            name,
            text,
            template_id,
            owner_id,
        )
        return result == "UPDATE 1"
    except asyncpg.UniqueViolationError:
        return False


# ── Scheduled broadcasts ──────────────────────────────────────────────────


async def create_scheduled(
    pool: asyncpg.Pool, bot_id: int, text: str, execute_at, created_by: int,
    repeat_interval_min: int = 0,
) -> int:
    """Создать отложенную рассылку. repeat_interval_min>0 → повторяемая: после
    исполнения reschedule_if_recurring создаёт следующее вхождение через интервал."""
    try:
        repeat_interval_min = max(0, int(repeat_interval_min or 0))
    except (TypeError, ValueError):
        repeat_interval_min = 0
    return await pool.fetchval(
        """INSERT INTO scheduled_broadcasts
               (bot_id, message_text, execute_at, created_by, repeat_interval_min)
           VALUES ($1,$2,$3,$4,$5) RETURNING id""",
        bot_id,
        text,
        execute_at,
        created_by,
        repeat_interval_min,
    )


async def get_pending_scheduled(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        """SELECT s.*, m.token FROM scheduled_broadcasts s
           JOIN managed_bots m ON m.bot_id=s.bot_id AND m.is_active=true
           WHERE s.status='pending' AND s.execute_at <= NOW()
           ORDER BY s.execute_at ASC LIMIT 100""",
    )
    return _dec_bot_rows(rows)


async def mark_scheduled_done(pool: asyncpg.Pool, schedule_id: int) -> None:
    await pool.execute(
        "UPDATE scheduled_broadcasts SET status='done' WHERE id=$1", schedule_id
    )


async def reschedule_if_recurring(pool: asyncpg.Pool, row: dict) -> int | None:
    """Если расписание повторяемое (repeat_interval_min>0) — создаёт следующее
    вхождение через интервал от execute_at (но не в прошлом). Возвращает id нового
    вхождения или None. Идемпотентно относительно исходной строки (она уже done/missed).
    """
    try:
        interval = int(row.get("repeat_interval_min") or 0)
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        return None
    import datetime as _dt
    base = row.get("execute_at")
    now = _dt.datetime.now(_dt.timezone.utc)
    if base is None:
        base = now
    elif base.tzinfo is None:
        base = base.replace(tzinfo=_dt.timezone.utc)
    # Следующее вхождение: base + interval; если оно уже в прошлом (планировщик
    # отставал), проматываем вперёд кратно интервалу до будущего.
    nxt = base + _dt.timedelta(minutes=interval)
    if nxt <= now:
        missed = int((now - nxt).total_seconds() // (interval * 60)) + 1
        nxt = nxt + _dt.timedelta(minutes=interval * missed)
    try:
        new_id = await pool.fetchval(
            "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, repeat_interval_min) "
            "VALUES($1,$2,$3,$4,$5) RETURNING id",
            row["bot_id"], row["message_text"], nxt, row["created_by"], interval,
        )
        return int(new_id) if new_id is not None else None
    except Exception:
        log.exception("reschedule_if_recurring failed for sched=%s", row.get("id"))
        return None


async def cancel_scheduled(pool: asyncpg.Pool, schedule_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        """UPDATE scheduled_broadcasts SET status='cancelled'
           WHERE id=$1 AND created_by=$2 AND status='pending'""",
        schedule_id,
        owner_id,
    )
    return result == "UPDATE 1"


async def get_bot_schedules(
    pool: asyncpg.Pool, bot_id: int, limit: int = 10
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM scheduled_broadcasts WHERE bot_id=$1 ORDER BY execute_at DESC LIMIT $2",
        bot_id,
        limit,
    )


# ── Auto-replies ──────────────────────────────────────────────────────────


async def get_auto_replies(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM auto_replies WHERE bot_id=$1 ORDER BY id", bot_id
    )


async def get_active_auto_replies(
    pool: asyncpg.Pool, bot_id: int
) -> list[asyncpg.Record]:
    # priority DESC — правило с большим приоритетом выигрывает (цикл в
    # auto_responder — «первое совпадение выигрывает»). Фолбэк на ORDER BY id,
    # если колонка priority ещё не мигрирована (schema_v142).
    try:
        return await pool.fetch(
            "SELECT * FROM auto_replies WHERE bot_id=$1 AND is_active=true "
            "ORDER BY priority DESC, id",
            bot_id,
        )
    except asyncpg.UndefinedColumnError:
        return await pool.fetch(
            "SELECT * FROM auto_replies WHERE bot_id=$1 AND is_active=true ORDER BY id",
            bot_id,
        )


async def add_auto_reply(
    pool: asyncpg.Pool,
    bot_id: int,
    trigger_type: str,
    keyword: str | None,
    response_text: str,
) -> asyncpg.Record:
    return await pool.fetchrow(
        "INSERT INTO auto_replies(bot_id,trigger_type,keyword,response_text) VALUES($1,$2,$3,$4) RETURNING id",
        bot_id,
        trigger_type,
        keyword,
        response_text,
    )


async def toggle_auto_reply(pool: asyncpg.Pool, reply_id: int, bot_id: int) -> str:
    return await pool.execute(
        "UPDATE auto_replies SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2",
        reply_id,
        bot_id,
    )


async def delete_auto_reply(pool: asyncpg.Pool, reply_id: int, bot_id: int) -> str:
    return await pool.execute(
        "DELETE FROM auto_replies WHERE id=$1 AND bot_id=$2", reply_id, bot_id
    )


# ── Update offsets ────────────────────────────────────────────────────────


async def get_update_offset(pool: asyncpg.Pool, bot_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT last_update_id FROM bot_update_offsets WHERE bot_id=$1", bot_id
    )
    return row["last_update_id"] if row else 0


async def set_update_offset(pool: asyncpg.Pool, bot_id: int, offset: int) -> None:
    await pool.execute(
        "INSERT INTO bot_update_offsets(bot_id,last_update_id) VALUES($1,$2) "
        "ON CONFLICT(bot_id) DO UPDATE SET last_update_id=$2",
        bot_id,
        offset,
    )


async def get_bots_with_auto_replies(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT DISTINCT b.bot_id, b.token FROM managed_bots b "
        "JOIN auto_replies ar ON ar.bot_id=b.bot_id WHERE ar.is_active=true"
    )
    return _dec_bot_rows(rows)


async def get_bots_for_polling(pool: asyncpg.Pool) -> list[dict]:
    """Return all active managed bots for polling (activity, deep links, swarm, A/B, etc.)."""
    rows = await pool.fetch(
        "SELECT bot_id, token FROM managed_bots WHERE is_active=true"
    )
    return _dec_bot_rows(rows)


# ── Hermes Relay ───────────────────────────────────────────────────────────


async def enable_relay(
    pool: asyncpg.Pool, bot_id: int, enabled: bool, added_by: int | None = None
) -> None:
    if added_by is not None:
        await pool.execute(
            "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2 AND added_by=$3",
            enabled,
            bot_id,
            added_by,
        )
    else:
        await pool.execute(
            "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2", enabled, bot_id
        )


async def get_bots_with_relay(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT bot_id, token, added_by FROM managed_bots "
        "WHERE relay_enabled=true AND is_active=true"
    )
    return _dec_bot_rows(rows)


async def get_or_create_relay_session(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> int:
    row = await pool.fetchrow(
        "INSERT INTO relay_sessions(bot_id,user_id,username,first_name,last_activity) "
        "VALUES($1,$2,$3,$4,NOW()) "
        "ON CONFLICT (bot_id,user_id) DO UPDATE SET "
        "last_activity=NOW(), username=$3, first_name=$4, "
        "messages_count=relay_sessions.messages_count+1 "
        "RETURNING id",
        bot_id,
        user_id,
        username,
        first_name,
    )
    return row["id"]


async def save_relay_message(
    pool: asyncpg.Pool,
    session_id: int,
    direction: str,
    text: str,
    forwarded_msg_id: int | None = None,
) -> None:
    await pool.execute(
        "INSERT INTO relay_messages(session_id,direction,text,forwarded_msg_id) "
        "VALUES($1,$2,$3,$4)",
        session_id,
        direction,
        text,
        forwarded_msg_id,
    )


async def find_session_by_forwarded_msg(
    pool: asyncpg.Pool, forwarded_msg_id: int
) -> dict | None:
    row = await pool.fetchrow(
        """SELECT rs.bot_id, rs.user_id, mb.token
           FROM relay_messages rm
           JOIN relay_sessions rs ON rs.id = rm.session_id
           JOIN managed_bots mb ON mb.bot_id = rs.bot_id
           WHERE rm.forwarded_msg_id=$1""",
        forwarded_msg_id,
    )
    if row is None:
        return None
    return _dec_bot_rows([row])[0]


async def get_relay_sessions(
    pool: asyncpg.Pool, bot_id: int, limit: int = 5
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT rs.id, rs.user_id, rs.username, rs.first_name, rs.last_activity, rs.messages_count,
                  (SELECT text FROM relay_messages WHERE session_id=rs.id
                   ORDER BY created_at DESC LIMIT 1) as last_text
           FROM relay_sessions rs WHERE rs.bot_id=$1
           ORDER BY rs.last_activity DESC LIMIT $2""",
        bot_id,
        limit,
    )


async def get_relay_session_messages(
    pool: asyncpg.Pool, session_id: int, limit: int = 20
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT direction, text AS message_text, created_at
           FROM relay_messages WHERE session_id=$1
           ORDER BY created_at DESC LIMIT $2""",
        session_id,
        limit,
    )


async def close_relay_session(pool: asyncpg.Pool, session_id: int) -> None:
    await pool.execute("DELETE FROM relay_sessions WHERE id=$1", session_id)


# ── Funnels ────────────────────────────────────────────────────────────────


async def get_funnels(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM funnels WHERE bot_id=$1 ORDER BY id", bot_id)


async def get_active_funnels(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id
    )


async def create_funnel(
    pool: asyncpg.Pool,
    bot_id: int,
    name: str,
    trigger_type: str,
    keyword: str | None = None,
) -> asyncpg.Record:
    return await pool.fetchrow(
        "INSERT INTO funnels(bot_id,name,trigger_type,keyword) VALUES($1,$2,$3,$4) RETURNING id",
        bot_id,
        name,
        trigger_type,
        keyword,
    )


async def delete_funnel(pool: asyncpg.Pool, funnel_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM funnels WHERE id=$1 AND bot_id=$2", funnel_id, bot_id
    )


async def toggle_funnel(pool: asyncpg.Pool, funnel_id: int, bot_id: int) -> None:
    await pool.execute(
        "UPDATE funnels SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2",
        funnel_id,
        bot_id,
    )


async def get_funnel_steps(pool: asyncpg.Pool, funnel_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order",
        funnel_id,
    )


async def add_funnel_step(
    pool: asyncpg.Pool,
    funnel_id: int,
    step_order: int,
    message_text: str,
    delay_minutes: int,
) -> None:
    await pool.execute(
        "INSERT INTO funnel_steps(funnel_id,step_order,message_text,delay_minutes) VALUES($1,$2,$3,$4)"
        " ON CONFLICT(funnel_id,step_order) DO UPDATE SET message_text=$3,delay_minutes=$4",
        funnel_id,
        step_order,
        message_text,
        delay_minutes,
    )


async def copy_funnels(pool: asyncpg.Pool, from_bot_id: int, to_bot_id: int) -> int:
    """Copy all funnels (with steps) from one bot to another. Returns count of copied funnels."""
    funnels = await pool.fetch("SELECT * FROM funnels WHERE bot_id=$1", from_bot_id)
    count = 0
    for f in funnels:
        try:
            new_funnel = await pool.fetchrow(
                "INSERT INTO funnels(bot_id, name, trigger_type, keyword) VALUES($1,$2,$3,$4) RETURNING id",
                to_bot_id,
                f["name"],
                f["trigger_type"],
                f["keyword"],
            )
            steps = await pool.fetch(
                "SELECT * FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order", f["id"]
            )
            for s in steps:
                try:
                    await pool.execute(
                        "INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes) VALUES($1,$2,$3,$4)",
                        new_funnel["id"],
                        s["step_order"],
                        s["message_text"],
                        s["delay_minutes"],
                    )
                except Exception:
                    log.debug("copy_funnels: skip step for funnel %s: %s", f["id"], s["step_order"])
            count += 1
        except Exception:
            log.debug("copy_funnels: skip funnel %s", f["id"], exc_info=True)
    return count


async def get_funnel_subscriber_ids(pool: asyncpg.Pool, funnel_id: int) -> list[int]:
    """Return user_ids of all active (not completed) funnel subscribers."""
    rows = await pool.fetch(
        "SELECT user_id FROM funnel_subscriptions WHERE funnel_id=$1 AND completed=false",
        funnel_id,
    )
    return [r["user_id"] for r in rows]


async def subscribe_to_funnel(pool: asyncpg.Pool, funnel_id: int, user_id: int) -> None:
    """Subscribe user to funnel.

    Idempotent: if user is already active in the funnel (completed=false) the
    row is left untouched so in-progress users are not reset back to step 0.
    If the user previously completed the funnel they are re-enrolled from the
    beginning.

    Respects the first step's delay_minutes so step-0 doesn't fire prematurely.
    Increments the funnel's entered_count counter.
    """
    from datetime import datetime, timedelta, timezone

    # Look up first step's delay so next_send_at is accurate from the start
    first_step = await pool.fetchrow(
        "SELECT delay_minutes FROM funnel_steps WHERE funnel_id=$1 AND step_order=0",
        funnel_id,
    )
    first_delay = int(first_step["delay_minutes"]) if first_step else 0
    first_send_at = datetime.now(timezone.utc) + timedelta(minutes=first_delay)

    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """INSERT INTO funnel_subscriptions(funnel_id, user_id, next_send_at)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (funnel_id, user_id) DO UPDATE
                       SET current_step  = 0,
                           completed      = false,
                           dropped        = false,
                           next_send_at   = $3
                       WHERE funnel_subscriptions.completed = true
                          OR funnel_subscriptions.dropped = true""",
                funnel_id,
                user_id,
                first_send_at,
            )
            if result == "INSERT 0 1":
                try:
                    await conn.execute(
                        "UPDATE funnels SET entered_count = entered_count + 1 WHERE id=$1",
                        funnel_id,
                    )
                except Exception:
                    pass  # column may not exist yet — schema migration will add it


async def get_due_funnel_steps(pool: asyncpg.Pool) -> list[dict]:
    """Returns subscriptions where next step is due (excluding completed and dropped)."""
    rows = await pool.fetch(
        """SELECT fs.id as sub_id, fs.funnel_id, fs.user_id, fs.current_step,
                  fst.message_text, fst.delay_minutes,
                  f.bot_id, mb.token,
                  (SELECT COUNT(*) FROM funnel_steps WHERE funnel_id=fs.funnel_id) as total_steps
           FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id AND f.is_active=true
           JOIN funnel_steps fst ON fst.funnel_id=fs.funnel_id AND fst.step_order=fs.current_step
           JOIN managed_bots mb ON mb.bot_id=f.bot_id AND mb.is_active=true
           WHERE fs.completed=false
             AND COALESCE(fs.dropped, false) = false
             AND fs.next_send_at<=now()""",
    )
    return _dec_bot_rows(rows)


async def advance_funnel_step(
    pool: asyncpg.Pool,
    sub_id: int,
    next_step: int,
    total_steps: int,
    delay_minutes: int,
    funnel_id: int | None = None,
) -> None:
    if next_step >= total_steps:
        await pool.execute(
            "UPDATE funnel_subscriptions SET completed=true, completed_at=now() WHERE id=$1",
            sub_id,
        )
        # Increment completed_count on the funnel
        if funnel_id is not None:
            try:
                await pool.execute(
                    "UPDATE funnels SET completed_count = completed_count + 1 WHERE id=$1",
                    funnel_id,
                )
            except Exception:
                pass  # column may not exist yet — schema migration will add it
    else:
        from datetime import datetime, timedelta, timezone

        next_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        await pool.execute(
            "UPDATE funnel_subscriptions SET current_step=$2, next_send_at=$3 WHERE id=$1",
            sub_id,
            next_step,
            next_at,
        )


async def unsubscribe_user_from_funnels(
    pool: asyncpg.Pool, bot_id: int, user_id: int
) -> int:
    """Снять пользователя со всех активных воронок этого бота. Возвращает число.

    До этого выйти из drip-цепочки было НЕЧЕМ: единственным способом перестать
    получать шаги было заблокировать бота (403 → dropped). Человек, написавший
    «стоп», продолжал получать цепочку — это и причина жалоб на спам, и потеря
    контакта для владельца бота, потому что блокировка необратима.

    Помечаем dropped (а не completed): воронка НЕ пройдена, и статистика
    завершений не должна врать. Тот же признак ставит funnel_runner при 403,
    поэтому get_due_funnel_steps такие подписки уже не выбирает.
    """
    rows = await pool.fetch(
        """UPDATE funnel_subscriptions fs
              SET dropped = true, completed_at = now()
             FROM funnels f
            WHERE fs.funnel_id = f.id
              AND f.bot_id = $1
              AND fs.user_id = $2
              AND fs.completed = false
              AND fs.dropped = false
        RETURNING fs.funnel_id""",
        bot_id, user_id,
    )
    for r in rows or []:
        try:
            await pool.execute(
                "UPDATE funnels SET dropped_count = dropped_count + 1 WHERE id=$1",
                r["funnel_id"],
            )
        except Exception:
            pass  # счётчик — наблюдаемость, не должен ломать отписку
    return len(rows or [])


async def get_bot_stats(pool: asyncpg.Pool, bot_id: int) -> dict:
    """Get aggregated statistics for a bot (concurrent queries)."""
    with timed(log, "get_bot_stats", extra={"bot_id": bot_id}):
        queries = [
            ("SELECT COUNT(*) FROM relay_sessions WHERE bot_id=$1", (bot_id,)),
            ("SELECT COUNT(*) FROM relay_messages rm JOIN relay_sessions rs ON rs.id=rm.session_id WHERE rs.bot_id=$1 AND rm.direction='in'", (bot_id,)),
            ("SELECT COUNT(*) FROM relay_messages rm JOIN relay_sessions rs ON rs.id=rm.session_id WHERE rs.bot_id=$1 AND rm.direction='out'", (bot_id,)),
            ("SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=true", (bot_id,)),
            ("SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", (bot_id,)),
            ("SELECT COUNT(DISTINCT fs.user_id) FROM funnel_subscriptions fs JOIN funnels f ON f.id=fs.funnel_id WHERE f.bot_id=$1", (bot_id,)),
            ("SELECT COUNT(*) FROM funnel_subscriptions fs JOIN funnels f ON f.id=fs.funnel_id WHERE f.bot_id=$1 AND fs.completed=true", (bot_id,)),
            ("SELECT COUNT(*) FROM funnel_subscriptions fs JOIN funnels f ON f.id=fs.funnel_id WHERE f.bot_id=$1", (bot_id,)),
            ("SELECT COUNT(*) FROM funnel_subscriptions fs JOIN funnels f ON f.id=fs.funnel_id WHERE f.bot_id=$1 AND COALESCE(fs.dropped, false) = true", (bot_id,)),
            ("SELECT COUNT(*) FROM relay_sessions WHERE bot_id=$1 AND last_activity >= NOW() - INTERVAL '24 hours'", (bot_id,)),
            ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", (bot_id,)),
            ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '24 hours'", (bot_id,)),
            ("SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '7 days'", (bot_id,)),
            ("SELECT COUNT(*) FROM broadcasts WHERE bot_id=$1", (bot_id,)),
            ("SELECT COALESCE(SUM(sent_count), 0) FROM broadcasts WHERE bot_id=$1", (bot_id,)),
        ]
        results = await run_concurrent_queries(pool, queries)
        _v = lambda r: (r[0][0] if r else 0) or 0
    return {
        "relay_sessions": _v(results[0]),
        "msg_in": _v(results[1]),
        "msg_out": _v(results[2]),
        "active_replies": _v(results[3]),
        "active_funnels": _v(results[4]),
        "funnel_users": _v(results[5]),
        "funnel_completed": _v(results[6]),
        "funnel_total_subs": _v(results[7]),
        "funnel_dropped": _v(results[8]),
        "relay_today": _v(results[9]),
        "aud_total": _v(results[10]),
        "aud_today": _v(results[11]),
        "aud_week": _v(results[12]),
        "broadcasts_total": _v(results[13]),
        "broadcasts_sent": _v(results[14]),
    }


async def get_bots_with_funnels(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT DISTINCT b.bot_id, b.token, b.added_by FROM managed_bots b "
        "JOIN funnels f ON f.bot_id=b.bot_id WHERE f.is_active=true AND b.is_active=true"
    )
    return _dec_bot_rows(rows)


async def update_bot_token(
    pool: asyncpg.Pool,
    bot_id: int,
    added_by: int,
    new_token: str,
    new_bot_id: int,
    username: str,
    first_name: str,
) -> None:
    from services.token_vault import encrypt_token as _enc_tok
    await pool.execute(
        """UPDATE managed_bots
           SET token=$3, bot_id=$4, username=$5, first_name=$6
           WHERE bot_id=$1 AND added_by=$2""",
        bot_id,
        added_by,
        _enc_tok(new_token),
        new_bot_id,
        username,
        first_name,
    )


async def get_audience_daily_growth(
    pool: asyncpg.Pool, bot_id: int, days: int = 7
) -> list[dict]:
    """Returns list of {date, new_users} for the last N days."""
    rows = await pool.fetch(
        """SELECT DATE(first_seen AT TIME ZONE 'UTC') AS d, COUNT(*) AS cnt
           FROM bot_users
           WHERE bot_id=$1 AND first_seen >= NOW() - ($2 || ' days')::INTERVAL
           GROUP BY d ORDER BY d""",
        bot_id,
        str(days),
    )
    return [{"date": r["d"], "count": r["cnt"]} for r in rows]


async def get_audience_new_users(
    pool: asyncpg.Pool, bot_id: int, days: int
) -> list[int]:
    """Return user_ids of active users who joined within the last N days."""
    rows = await pool.fetch(
        """SELECT user_id FROM bot_users
           WHERE bot_id=$1 AND is_active=TRUE
             AND first_seen >= NOW() - ($2 || ' days')::INTERVAL""",
        bot_id,
        str(days),
    )
    return [r["user_id"] for r in rows]


async def get_audience_by_language(
    pool: asyncpg.Pool, bot_id: int, lang_code: str
) -> list[int]:
    """Return user_ids filtered by language_code."""
    rows = await pool.fetch(
        "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND language_code=$2",
        bot_id,
        lang_code,
    )
    return [r["user_id"] for r in rows]


async def get_audience_languages(pool: asyncpg.Pool, bot_id: int) -> list[dict]:
    """Return list of {lang, count} sorted by count desc."""
    rows = await pool.fetch(
        """SELECT COALESCE(language_code, 'unknown') AS lang, COUNT(*) AS cnt
           FROM bot_users WHERE bot_id=$1 AND is_active=TRUE
           GROUP BY lang ORDER BY cnt DESC LIMIT 10""",
        bot_id,
    )
    return [{"lang": r["lang"], "count": r["cnt"]} for r in rows]


async def copy_auto_replies(
    pool: asyncpg.Pool, from_bot_id: int, to_bot_id: int
) -> int:
    """Copy all auto-replies from one bot to another. Returns count of copied rules."""
    rules = await pool.fetch(
        "SELECT trigger_type, keyword, response_text FROM auto_replies WHERE bot_id=$1",
        from_bot_id,
    )
    count = 0
    for r in rules:
        try:
            await pool.execute(
                """INSERT INTO auto_replies (bot_id, trigger_type, keyword, response_text)
                   VALUES ($1, $2, $3, $4)""",
                to_bot_id,
                r["trigger_type"],
                r["keyword"],
                r["response_text"],
            )
            count += 1
        except Exception as e:
            log.debug("copy_auto_replies: skip duplicate rule: %s", e)
    return count


# ── Swarm / Mode System ─────────────────────────────────────────────────


async def get_system_mode(pool: asyncpg.Pool) -> str:
    row = await pool.fetchrow("SELECT mode FROM system_mode WHERE id=1")
    return row["mode"] if row else "manual"


async def set_system_mode(pool: asyncpg.Pool, mode: str) -> None:
    _valid_modes = {"manual", "assisted", "autopilot", "growth", "experiment", "stability"}
    if mode not in _valid_modes:
        raise ValueError(f"Invalid mode: {mode!r}. Must be one of: {', '.join(sorted(_valid_modes))}")
    await pool.execute(
        "UPDATE system_mode SET mode=$1, updated_at=now() WHERE id=1", mode
    )


async def set_bot_role(
    pool: asyncpg.Pool,
    bot_id: int,
    role: str,
    cluster: str = "default",
    added_by: int | None = None,
) -> None:
    _valid_roles = {"conversion", "retention", "general"}
    if role not in _valid_roles:
        raise ValueError(f"Invalid role: {role!r}. Must be one of: {', '.join(sorted(_valid_roles))}")
    if added_by is not None:
        await pool.execute(
            "UPDATE managed_bots SET bot_role=$2, cluster=$3 WHERE bot_id=$1 AND added_by=$4",
            bot_id,
            role,
            cluster,
            added_by,
        )
    else:
        await pool.execute(
            "UPDATE managed_bots SET bot_role=$2, cluster=$3 WHERE bot_id=$1",
            bot_id,
            role,
            cluster,
        )


async def toggle_swarm(
    pool: asyncpg.Pool, bot_id: int, enabled: bool, added_by: int | None = None
) -> None:
    if added_by is not None:
        await pool.execute(
            "UPDATE managed_bots SET swarm_enabled=$2 WHERE bot_id=$1 AND added_by=$3",
            bot_id,
            enabled,
            added_by,
        )
    else:
        await pool.execute(
            "UPDATE managed_bots SET swarm_enabled=$2 WHERE bot_id=$1", bot_id, enabled
        )


async def get_swarm_bots(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.*, bm.score, bm.ctr, bm.conversion_rate, bm.retention_d1
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND m.swarm_enabled=TRUE
           ORDER BY COALESCE(bm.score, 0) DESC""",
        added_by,
    )


async def update_bot_metrics(
    pool: asyncpg.Pool,
    bot_id: int,
    ctr: float,
    conversion: float,
    retention_d1: float,
    retention_d7: float,
) -> None:
    score = ctr * 0.3 + conversion * 0.4 + retention_d1 * 0.2 + retention_d7 * 0.1
    await pool.execute(
        """INSERT INTO bot_metrics (bot_id, ctr, conversion_rate, retention_d1, retention_d7, score)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (bot_id) DO UPDATE SET
               ctr=$2, conversion_rate=$3, retention_d1=$4,
               retention_d7=$5, score=$6, updated_at=now()""",
        bot_id,
        ctr,
        conversion,
        retention_d1,
        retention_d7,
        score,
    )


# ── CRM Tags ──────────────────────────────────────────────────────────────


async def add_user_tag(pool, bot_id: int, user_id: int, tag: str) -> bool:
    """Returns True if tag was new."""
    try:
        await pool.execute(
            "INSERT INTO user_tags(bot_id,user_id,tag) VALUES($1,$2,$3)",
            bot_id,
            user_id,
            tag,
        )
        return True
    except Exception:
        log.debug(
            "add_user_tag: error for bot_id=%s user_id=%s tag=%s",
            bot_id,
            user_id,
            tag,
            exc_info=True,
        )
        return False


async def remove_user_tag(pool, bot_id: int, user_id: int, tag: str) -> None:
    await pool.execute(
        "DELETE FROM user_tags WHERE bot_id=$1 AND user_id=$2 AND tag=$3",
        bot_id,
        user_id,
        tag,
    )


async def get_user_tags(pool, bot_id: int, user_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT tag FROM user_tags WHERE bot_id=$1 AND user_id=$2 ORDER BY tag",
        bot_id,
        user_id,
    )
    return [r["tag"] for r in rows]


async def get_tag_names(pool, bot_id: int) -> list[dict]:
    """All unique tags for this bot with counts.

    user_id=0 rows are standalone tag definitions (not assigned to any user).
    We count them as 0 users so the tag appears in the list but shows 0 members.
    """
    rows = await pool.fetch(
        """SELECT tag,
                  COUNT(*) FILTER (WHERE user_id != 0) AS cnt
           FROM user_tags WHERE bot_id=$1
           GROUP BY tag ORDER BY cnt DESC LIMIT 30""",
        bot_id,
    )
    return [{"tag": r["tag"], "count": r["cnt"]} for r in rows]


async def get_users_by_tag(pool, bot_id: int, tag: str) -> list[int]:
    # user_id=0 is the sentinel for a standalone tag definition (no user assigned yet).
    rows = await pool.fetch(
        "SELECT user_id FROM user_tags WHERE bot_id=$1 AND tag=$2 AND user_id != 0",
        bot_id,
        tag,
    )
    return [r["user_id"] for r in rows]


# ── Automation Rules ───────────────────────────────────────────────────────


async def get_automation_rules(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM automation_rules WHERE bot_id=$1 ORDER BY id",
        bot_id,
    )


async def get_active_automation_rules(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM automation_rules WHERE bot_id=$1 AND is_active=TRUE",
        bot_id,
    )


async def add_automation_rule(
    pool,
    bot_id: int,
    name: str,
    trigger_type: str,
    trigger_value,
    action_type: str,
    action_value: str,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO automation_rules(bot_id,name,trigger_type,trigger_value,action_type,action_value)
           VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
        bot_id,
        name,
        trigger_type,
        trigger_value,
        action_type,
        action_value,
    )
    return row["id"]


async def toggle_automation_rule(pool, rule_id: int, bot_id: int) -> None:
    await pool.execute(
        "UPDATE automation_rules SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2",
        rule_id,
        bot_id,
    )


async def delete_automation_rule(pool, rule_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM automation_rules WHERE id=$1 AND bot_id=$2",
        rule_id,
        bot_id,
    )


# ── A/B Experiments ────────────────────────────────────────────────────────


async def get_experiments(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM experiments WHERE bot_id=$1 ORDER BY id DESC", bot_id
    )


async def get_experiment(
    pool, exp_id: int, bot_id: int | None = None
) -> asyncpg.Record | None:
    if bot_id is not None:
        return await pool.fetchrow(
            "SELECT * FROM experiments WHERE id=$1 AND bot_id=$2", exp_id, bot_id
        )
    return await pool.fetchrow("SELECT * FROM experiments WHERE id=$1", exp_id)


async def get_experiment_variants(pool, exp_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1 ORDER BY id", exp_id
    )


async def create_experiment(pool, bot_id: int, name: str, exp_type: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO experiments(bot_id,name,experiment_type) VALUES($1,$2,$3) RETURNING id",
        bot_id,
        name,
        exp_type,
    )
    return row["id"]


async def add_experiment_variant(
    pool, exp_id: int, name: str, content: str, weight: int = 50
) -> int:
    row = await pool.fetchrow(
        "INSERT INTO experiment_variants(experiment_id,name,content,weight) VALUES($1,$2,$3,$4) RETURNING id",
        exp_id,
        name,
        content,
        weight,
    )
    return row["id"]


async def set_experiment_status(
    pool, exp_id: int, status: str, bot_id: int | None = None
) -> None:
    # Stamp started_at when activating, ended_at when finishing
    extra_sql = ""
    if status == "active":
        extra_sql = ", started_at = COALESCE(started_at, NOW())"
    elif status in ("completed", "paused"):
        extra_sql = ", ended_at = NOW()"

    if bot_id is not None:
        await pool.execute(
            f"UPDATE experiments SET status=$2{extra_sql} WHERE id=$1 AND bot_id=$3",
            exp_id,
            status,
            bot_id,
        )
    else:
        await pool.execute(
            f"UPDATE experiments SET status=$2{extra_sql} WHERE id=$1", exp_id, status
        )


async def get_active_experiment(pool, bot_id: int, exp_type: str = "start_message"):
    return await pool.fetchrow(
        "SELECT * FROM experiments WHERE bot_id=$1 AND experiment_type=$2 AND status='active' LIMIT 1",
        bot_id,
        exp_type,
    )


async def assign_experiment_variant(
    pool, bot_id: int, user_id: int, exp_id: int
) -> asyncpg.Record | None:
    """Assign user to variant using weighted random. Returns variant record."""

    existing = await pool.fetchrow(
        """SELECT ea.*, ev.content, ev.name as variant_name
           FROM experiment_assignments ea
           JOIN experiment_variants ev ON ev.id=ea.variant_id
           WHERE ea.bot_id=$1 AND ea.user_id=$2 AND ea.experiment_id=$3""",
        bot_id,
        user_id,
        exp_id,
    )
    if existing:
        return existing

    variants = await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1", exp_id
    )
    if not variants:
        return None

    # Weighted random selection
    total_weight = sum(v["weight"] for v in variants)
    r = random.randint(1, total_weight)
    cumulative = 0
    chosen = variants[0]
    for v in variants:
        cumulative += v["weight"]
        if r <= cumulative:
            chosen = v
            break

    try:
        await pool.execute(
            "INSERT INTO experiment_assignments(bot_id,user_id,experiment_id,variant_id) "
            "VALUES($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING",
            bot_id,
            user_id,
            exp_id,
            chosen["id"],
        )
        await pool.execute(
            "UPDATE experiment_variants SET impressions=impressions+1 WHERE id=$1",
            chosen["id"],
        )
    except Exception as e:
        log.debug(
            "assign_experiment_variant: skip (likely duplicate assignment): %s", e
        )
    return chosen


async def record_experiment_conversion(
    pool, bot_id: int, user_id: int, exp_id: int
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            assignment = await conn.fetchrow(
                "SELECT * FROM experiment_assignments WHERE bot_id=$1 AND user_id=$2 AND experiment_id=$3 AND converted=FALSE FOR UPDATE",
                bot_id,
                user_id,
                exp_id,
            )
            if assignment:
                await conn.execute(
                    "UPDATE experiment_assignments SET converted=TRUE, converted_at=NOW() WHERE id=$1",
                    assignment["id"],
                )
                await conn.execute(
                    "UPDATE experiment_variants SET conversions=conversions+1 WHERE id=$1",
                    assignment["variant_id"],
                )


async def check_experiment_winner(pool, exp_id: int) -> int | None:
    """If any variant has >= min_sample_size and highest CTR, return its id. Else None."""
    exp = await pool.fetchrow("SELECT * FROM experiments WHERE id=$1", exp_id)
    if not exp or exp["status"] != "active":
        return None
    variants = await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1", exp_id
    )
    candidates = [v for v in variants if v["impressions"] >= exp["min_sample_size"]]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda v: v["conversions"] / v["impressions"] if v["impressions"] else 0,
    )
    ctr = best["conversions"] / best["impressions"] if best["impressions"] else 0
    if ctr > 0:
        await pool.execute(
            "UPDATE experiments SET status='completed', winner_variant_id=$2, ended_at=NOW() WHERE id=$1",
            exp_id,
            best["id"],
        )
        return best["id"]
    return None


async def delete_experiment(pool, exp_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM experiments WHERE id=$1 AND bot_id=$2", exp_id, bot_id
    )


# ── Routing Engine ─────────────────────────────────────────────────────────


async def get_best_conversion_bot(
    pool, cluster: str, exclude_bot_id: int
) -> dict | None:
    """Get highest-scoring conversion/retention bot in the same cluster."""
    row = await pool.fetchrow(
        """SELECT m.bot_id, m.token, m.username, m.first_name,
                  COALESCE(bm.score, 0) as score
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.swarm_enabled=TRUE
             AND m.is_active=TRUE
             AND m.bot_role IN ('conversion', 'retention', 'general')
             AND m.cluster=$1
             AND m.bot_id != $2
           ORDER BY COALESCE(bm.score, 0) DESC
           LIMIT 1""",
        cluster,
        exclude_bot_id,
    )
    if row is None:
        return None
    return _dec_bot_rows([row])[0]


async def log_routing_decision(
    pool,
    from_bot_id: int,
    to_bot_id,
    user_id: int,
    decision: str,
    mode: str,
    score_from: float = 0,
    score_to: float = 0,
) -> None:
    await pool.execute(
        """INSERT INTO routing_log(from_bot_id,to_bot_id,user_id,decision,system_mode,score_from,score_to)
           VALUES($1,$2,$3,$4,$5,$6,$7)""",
        from_bot_id,
        to_bot_id,
        user_id,
        decision,
        mode,
        score_from,
        score_to,
    )


async def get_routing_stats(pool, bot_id: int, days: int = 7) -> dict:
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM routing_log WHERE from_bot_id=$1 AND created_at >= NOW()-($2||' days')::INTERVAL",
        bot_id,
        str(days),
    )
    routed = await pool.fetchval(
        "SELECT COUNT(*) FROM routing_log WHERE from_bot_id=$1 AND decision='routed' AND created_at >= NOW()-($2||' days')::INTERVAL",
        bot_id,
        str(days),
    )
    return {
        "total": total or 0,
        "routed": routed or 0,
        "kept": (total or 0) - (routed or 0),
    }


async def get_mode_routing_config(mode: str) -> dict:
    """Returns routing config based on system mode."""
    configs = {
        "manual": {
            "routing_enabled": False,
            "min_score_threshold": 0.0,
            "routing_probability": 0.0,
        },
        "assisted": {
            "routing_enabled": False,
            "min_score_threshold": 0.3,
            "routing_probability": 0.0,
        },
        "autopilot": {
            "routing_enabled": True,
            "min_score_threshold": 0.3,
            "routing_probability": 0.5,
        },
        "growth": {
            "routing_enabled": True,
            "min_score_threshold": 0.2,
            "routing_probability": 0.8,
        },
        "experiment": {
            "routing_enabled": True,
            "min_score_threshold": 0.1,
            "routing_probability": 1.0,
        },
        "stability": {
            "routing_enabled": False,
            "min_score_threshold": 0.5,
            "routing_probability": 0.0,
        },
    }
    return configs.get(mode, configs["manual"])


# ── Deep Links ──────────────────────────────────────────────────────────────


async def create_deep_link(pool, bot_id: int, name: str, start_param: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO bot_deep_links(bot_id,name,start_param) VALUES($1,$2,$3) RETURNING id",
        bot_id,
        name,
        start_param,
    )
    return row["id"]


async def get_deep_links(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM bot_deep_links WHERE bot_id=$1 ORDER BY click_count DESC",
        bot_id,
    )


async def get_deep_link_by_param(pool, bot_id: int, param: str):
    return await pool.fetchrow(
        "SELECT * FROM bot_deep_links WHERE bot_id=$1 AND start_param=$2",
        bot_id,
        param,
    )


async def record_deep_link_visit(
    pool, bot_id: int, param: str, user_id: int
) -> int | None:
    """Increments click_count, increments unique_users if first visit. Returns link_id or None."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            link = await conn.fetchrow(
                "SELECT id FROM bot_deep_links WHERE bot_id=$1 AND start_param=$2",
                bot_id,
                param,
            )
            if not link:
                return None
            link_id = link["id"]
            await conn.execute(
                "UPDATE bot_deep_links SET click_count=click_count+1 WHERE id=$1", link_id
            )
            try:
                result = await conn.execute(
                    "INSERT INTO deep_link_visits(link_id, user_id) VALUES($1, $2)",
                    link_id,
                    user_id,
                )
                if result == "INSERT 0 1":
                    await conn.execute(
                        "UPDATE bot_deep_links SET unique_users=unique_users+1 WHERE id=$1",
                        link_id,
                    )
            except asyncpg.UniqueViolationError:
                pass
    return link_id


async def delete_deep_link(pool, link_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM bot_deep_links WHERE id=$1 AND bot_id=$2", link_id, bot_id
    )


async def record_referral(
    pool,
    bot_id: int,
    referrer_user_id: int,
    referred_user_id: int,
    deep_link_id: int | None = None,
) -> bool:
    """Returns True if referral was new."""
    try:
        await pool.execute(
            """INSERT INTO referrals(bot_id,referrer_user_id,referred_user_id,deep_link_id)
               VALUES($1,$2,$3,$4) ON CONFLICT (bot_id, referred_user_id) DO NOTHING""",
            bot_id,
            referrer_user_id,
            referred_user_id,
            deep_link_id,
        )
        return True
    except Exception as e:
        log.debug("record_referral: skip (likely duplicate): %s", e)
        return False


async def get_referral_leaderboard(pool, bot_id: int, limit: int = 10) -> list:
    return await pool.fetch(
        """SELECT referrer_user_id, COUNT(*) as referral_count
           FROM referrals WHERE bot_id=$1
           GROUP BY referrer_user_id ORDER BY referral_count DESC LIMIT $2""",
        bot_id,
        limit,
    )


async def get_referral_total(pool, bot_id: int) -> int:
    return (
        await pool.fetchval(
            "SELECT COUNT(DISTINCT referred_user_id) FROM referrals WHERE bot_id=$1",
            bot_id,
        )
        or 0
    )


# ── User Activity ──────────────────────────────────────────────────────────


async def upsert_user_activity(pool, bot_id: int, user_id: int) -> bool:
    """Returns True if this is the user's first message (new user)."""
    row = await pool.fetchrow(
        """INSERT INTO user_activity(bot_id, user_id, message_count, last_seen, first_seen)
           VALUES($1, $2, 1, now(), now())
           ON CONFLICT (bot_id, user_id) DO UPDATE
           SET message_count = user_activity.message_count + 1,
               last_seen = now()
           RETURNING (xmax = 0) AS is_new""",
        bot_id,
        user_id,
    )
    return bool(row["is_new"]) if row else False


async def get_activity_segments(pool, bot_id: int) -> dict:
    """Returns counts: hot(<1d), warm(1-7d), cold(7-30d), lost(30d+)."""
    rows = await pool.fetch(
        """SELECT
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '1 day')   AS hot,
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '7 days'
                                AND last_seen <  now() - INTERVAL '1 day')   AS warm,
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '30 days'
                                AND last_seen <  now() - INTERVAL '7 days')  AS cold,
             COUNT(*) FILTER (WHERE last_seen <  now() - INTERVAL '30 days') AS lost,
             COUNT(*) AS total
           FROM user_activity WHERE bot_id=$1""",
        bot_id,
    )
    row = rows[0] if rows else {}
    return {
        "hot": int(row.get("hot", 0) or 0),
        "warm": int(row.get("warm", 0) or 0),
        "cold": int(row.get("cold", 0) or 0),
        "lost": int(row.get("lost", 0) or 0),
        "total": int(row.get("total", 0) or 0),
    }


async def get_inactive_user_ids(
    pool, bot_id: int, min_days: int, max_days: int | None = None
) -> list[int]:
    """Users not seen for min_days to max_days (None = no upper limit)."""
    if max_days is None:
        rows = await pool.fetch(
            """SELECT user_id FROM user_activity
               WHERE bot_id=$1 AND last_seen < now() - ($2 || ' days')::INTERVAL""",
            bot_id,
            str(min_days),
        )
    else:
        rows = await pool.fetch(
            """SELECT user_id FROM user_activity
               WHERE bot_id=$1
                 AND last_seen < now() - ($2 || ' days')::INTERVAL
                 AND last_seen >= now() - ($3 || ' days')::INTERVAL""",
            bot_id,
            str(min_days),
            str(max_days),
        )
    return [r["user_id"] for r in rows]


async def get_activity_heatmap(pool, bot_id: int, days: int = 7) -> list[dict]:
    """Message count per hour-of-day over last N days."""
    rows = await pool.fetch(
        """SELECT EXTRACT(HOUR FROM last_seen)::int AS hour, COUNT(*) AS cnt
           FROM user_activity
           WHERE bot_id=$1 AND last_seen >= now() - ($2 || ' days')::INTERVAL
           GROUP BY hour ORDER BY hour""",
        bot_id,
        str(days),
    )
    return [{"hour": r["hour"], "count": int(r["cnt"])} for r in rows]


async def get_top_active_users(pool, bot_id: int, limit: int = 10) -> list:
    return await pool.fetch(
        """SELECT user_id, message_count, last_seen
           FROM user_activity WHERE bot_id=$1
           ORDER BY message_count DESC LIMIT $2""",
        bot_id,
        limit,
    )


async def autotag_by_activity(pool, bot_id: int) -> dict:
    """Auto-tags users as activity:hot/warm/cold/lost. Returns counts."""
    segs = await get_activity_segments(pool, bot_id)

    async def _tag_segment(user_ids, tag):
        for uid in user_ids:
            try:
                await pool.execute(
                    "DELETE FROM user_tags WHERE bot_id=$1 AND user_id=$2 AND tag LIKE 'activity:%'",
                    bot_id,
                    uid,
                )
                await pool.execute(
                    "INSERT INTO user_tags(bot_id,user_id,tag) VALUES($1,$2,$3) ON CONFLICT (bot_id,user_id,tag) DO NOTHING",
                    bot_id,
                    uid,
                    tag,
                )
            except Exception as e:
                log.debug("autotag_by_activity: skip duplicate tag: %s", e)

    hot_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '1 day'",
        bot_id,
    )
    warm_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '7 days' AND last_seen < now() - INTERVAL '1 day'",
        bot_id,
    )
    cold_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '30 days' AND last_seen < now() - INTERVAL '7 days'",
        bot_id,
    )
    lost_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen < now() - INTERVAL '30 days'",
        bot_id,
    )
    await _tag_segment([r["user_id"] for r in hot_ids], "activity:hot")
    await _tag_segment([r["user_id"] for r in warm_ids], "activity:warm")
    await _tag_segment([r["user_id"] for r in cold_ids], "activity:cold")
    await _tag_segment([r["user_id"] for r in lost_ids], "activity:lost")
    return segs


# ── Keyword Analytics ──────────────────────────────────────────────────────


async def record_message_keywords(pool, bot_id: int, text: str) -> None:

    words = list(set(re.findall(r"[а-яёА-ЯЁa-zA-Z]{3,}", text.lower())))[:10]
    for word in words:
        try:
            await pool.execute(
                """INSERT INTO keyword_stats(bot_id, keyword, count, last_seen)
                   VALUES($1, $2, 1, now())
                   ON CONFLICT (bot_id, keyword) DO UPDATE
                   SET count = keyword_stats.count + 1, last_seen = now()""",
                bot_id,
                word,
            )
        except Exception:
            log.debug(
                "record_message_keywords: error recording keyword=%s bot_id=%s",
                word,
                bot_id,
                exc_info=True,
            )


async def get_top_keywords(pool, bot_id: int, limit: int = 20) -> list:
    return await pool.fetch(
        "SELECT keyword, count FROM keyword_stats WHERE bot_id=$1 ORDER BY count DESC LIMIT $2",
        bot_id,
        limit,
    )


async def get_keyword_stats_summary(pool, bot_id: int) -> dict:
    total_keywords = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM keyword_stats WHERE bot_id=$1", bot_id
        )
        or 0
    )
    total_messages = (
        await pool.fetchval(
            "SELECT SUM(count) FROM keyword_stats WHERE bot_id=$1", bot_id
        )
        or 0
    )
    return {
        "total_keywords": int(total_keywords),
        "total_messages": int(total_messages),
    }


# ── Network Management ──────────────────────────────────────────────────────


async def get_network_overview(pool: asyncpg.Pool, added_by: int) -> dict:
    """Aggregate stats across all active bots for a user."""
    meta = await pool.fetchrow(
        """SELECT COUNT(*) as total_bots,
                  SUM(CASE WHEN swarm_enabled THEN 1 ELSE 0 END) as swarm_bots,
                  COUNT(DISTINCT COALESCE(cluster,'default')) as clusters
           FROM managed_bots WHERE added_by=$1 AND is_active=TRUE""",
        added_by,
    )
    total_users = (
        await pool.fetchval(
            """SELECT COUNT(*) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""",
            added_by,
        )
        or 0
    )
    unique_users = (
        await pool.fetchval(
            """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""",
            added_by,
        )
        or 0
    )
    total_sent = (
        await pool.fetchval(
            """SELECT COALESCE(SUM(bc.sent_count),0) FROM broadcasts bc
           JOIN managed_bots m ON m.bot_id=bc.bot_id WHERE m.added_by=$1""",
            added_by,
        )
        or 0
    )
    avg_score = (
        await pool.fetchval(
            """SELECT AVG(bm.score) FROM bot_metrics bm
           JOIN managed_bots m ON m.bot_id=bm.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE""",
            added_by,
        )
        or 0
    )
    return {
        "total_bots": int(meta["total_bots"] or 0),
        "swarm_bots": int(meta["swarm_bots"] or 0),
        "clusters": int(meta["clusters"] or 0),
        "total_users": int(total_users),
        "unique_users": int(unique_users),
        "total_sent": int(total_sent),
        "avg_score": float(avg_score),
    }


async def get_cluster_list(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    """Return list of clusters with stats."""
    rows = await pool.fetch(
        """SELECT COALESCE(m.cluster,'default') as cluster,
                  COUNT(*) as bot_count,
                  SUM(CASE WHEN m.swarm_enabled THEN 1 ELSE 0 END) as swarm_count,
                  COALESCE(SUM(aud.cnt),0) as total_audience
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users
               WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           GROUP BY COALESCE(m.cluster,'default')
           ORDER BY total_audience DESC""",
        added_by,
    )
    return [dict(r) for r in rows]


async def get_bots_in_cluster(
    pool: asyncpg.Pool, added_by: int, cluster: str
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.*, COALESCE(aud.cnt,0) as audience_count, COALESCE(bm.score,0) as score
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND COALESCE(m.cluster,'default')=$2
           ORDER BY COALESCE(bm.score,0) DESC""",
        added_by,
        cluster,
    )


async def set_bot_cluster_name(
    pool: asyncpg.Pool, bot_id: int, added_by: int, cluster: str
) -> None:
    await pool.execute(
        "UPDATE managed_bots SET cluster=$3 WHERE bot_id=$1 AND added_by=$2",
        bot_id,
        added_by,
        cluster,
    )


async def bulk_set_swarm(
    pool: asyncpg.Pool, added_by: int, cluster: str, enabled: bool
) -> int:
    result = await pool.execute(
        """UPDATE managed_bots SET swarm_enabled=$3
           WHERE added_by=$1 AND COALESCE(cluster,'default')=$2 AND is_active=TRUE""",
        added_by,
        cluster,
        enabled,
    )
    return int(result.split()[-1])


async def bulk_set_role(
    pool: asyncpg.Pool, added_by: int, cluster: str, role: str
) -> int:
    result = await pool.execute(
        """UPDATE managed_bots SET bot_role=$3
           WHERE added_by=$1 AND COALESCE(cluster,'default')=$2 AND is_active=TRUE""",
        added_by,
        cluster,
        role,
    )
    return int(result.split()[-1])


async def get_routing_weights_for_user(
    pool: asyncpg.Pool, added_by: int
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.cluster,
                  m.bot_role, COALESCE(rw.weight, 1.0) as weight,
                  COALESCE(bm.score,0) as score
           FROM managed_bots m
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND m.swarm_enabled=TRUE
           ORDER BY m.cluster, weight DESC""",
        added_by,
    )


async def set_routing_weight(pool: asyncpg.Pool, bot_id: int, weight: float) -> None:
    await pool.execute(
        """INSERT INTO bot_routing_weights(bot_id, weight)
           VALUES($1,$2) ON CONFLICT(bot_id) DO UPDATE SET weight=$2, updated_at=NOW()""",
        bot_id,
        weight,
    )


async def reset_routing_weights(pool: asyncpg.Pool, added_by: int) -> None:
    await pool.execute(
        """DELETE FROM bot_routing_weights
           WHERE bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$1)""",
        added_by,
    )


async def get_bot_ranking(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.cluster,
                  m.bot_role, m.swarm_enabled,
                  COALESCE(aud.cnt,0) as audience,
                  COALESCE(bm.score,0) as score,
                  COALESCE(bm.ctr,0) as ctr,
                  COALESCE(bm.conversion_rate,0) as conversion_rate,
                  COALESCE(rw.weight,1.0) as weight
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           ORDER BY COALESCE(aud.cnt,0) DESC""",
        added_by,
    )


async def get_network_health(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.token, m.swarm_enabled, m.cluster,
                  COALESCE(o.last_update_id,0) as last_update_id,
                  COALESCE(aud.cnt,0) as audience
           FROM managed_bots m
           LEFT JOIN bot_update_offsets o ON o.bot_id=m.bot_id
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           ORDER BY aud.cnt DESC""",
        added_by,
    )
    return _dec_bot_rows(rows)


async def get_unique_network_users(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    """One (user_id, bot_id, token) per unique user. Picks most recently active bot."""
    rows = await pool.fetch(
        """SELECT DISTINCT ON (bu.user_id)
               bu.user_id, bu.bot_id, m.token
           FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
             AND bu.is_active=TRUE AND bu.is_blocked=FALSE
           ORDER BY bu.user_id, bu.last_seen DESC""",
        added_by,
    )
    return _dec_bot_rows(rows)


async def get_bot_overlap_stats(pool: asyncpg.Pool, added_by: int) -> dict:
    total_entries = (
        await pool.fetchval(
            """SELECT COUNT(*) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""",
            added_by,
        )
        or 0
    )
    unique_users = (
        await pool.fetchval(
            """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""",
            added_by,
        )
        or 0
    )
    multi_bot = (
        await pool.fetchval(
            """SELECT COUNT(*) FROM (
               SELECT bu.user_id FROM bot_users bu
               JOIN managed_bots m ON m.bot_id=bu.bot_id
               WHERE m.added_by=$1 AND bu.is_active=TRUE
               GROUP BY bu.user_id HAVING COUNT(DISTINCT bu.bot_id) > 1
           ) sub""",
            added_by,
        )
        or 0
    )
    return {
        "total_entries": int(total_entries),
        "unique_users": int(unique_users),
        "multi_bot_users": int(multi_bot),
        "overlap_pct": round(int(multi_bot) / int(unique_users) * 100, 1)
        if unique_users
        else 0,
    }


async def clone_bot_settings(pool: asyncpg.Pool, src_id: int, dst_id: int) -> dict:
    """Clone auto-replies, funnels (+steps), automation rules from src to dst."""
    counts = {"auto_replies": 0, "funnels": 0, "automation_rules": 0}
    replies = await pool.fetch(
        "SELECT trigger_type,keyword,response_text FROM auto_replies WHERE bot_id=$1",
        src_id,
    )
    for r in replies:
        try:
            await pool.execute(
                "INSERT INTO auto_replies(bot_id,trigger_type,keyword,response_text) VALUES($1,$2,$3,$4)",
                dst_id,
                r["trigger_type"],
                r["keyword"],
                r["response_text"],
            )
            counts["auto_replies"] += 1
        except Exception:
            log.debug(
                "clone_bot_settings: error copying auto_reply for dst_id=%s",
                dst_id,
                exc_info=True,
            )
    funnels_src = await pool.fetch(
        "SELECT id,name,trigger_type,keyword FROM funnels WHERE bot_id=$1",
        src_id,
    )
    for fn in funnels_src:
        try:
            new_fn = await pool.fetchrow(
                "INSERT INTO funnels(bot_id,name,trigger_type,keyword) VALUES($1,$2,$3,$4) RETURNING id",
                dst_id,
                fn["name"],
                fn["trigger_type"],
                fn["keyword"],
            )
            if new_fn:
                steps = await pool.fetch(
                    "SELECT step_order,message_text,delay_minutes FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order",
                    fn["id"],
                )
                for s in steps:
                    await pool.execute(
                        "INSERT INTO funnel_steps(funnel_id,step_order,message_text,delay_minutes) VALUES($1,$2,$3,$4)",
                        new_fn["id"],
                        s["step_order"],
                        s["message_text"],
                        s["delay_minutes"],
                    )
                counts["funnels"] += 1
        except Exception:
            log.debug(
                "clone_bot_settings: error copying funnel src=%s dst=%s",
                fn["id"],
                dst_id,
                exc_info=True,
            )
    rules = await pool.fetch(
        "SELECT name,trigger_type,trigger_value,action_type,action_value FROM automation_rules WHERE bot_id=$1",
        src_id,
    )
    for r in rules:
        try:
            await pool.execute(
                """INSERT INTO automation_rules(bot_id,name,trigger_type,trigger_value,action_type,action_value)
                   VALUES($1,$2,$3,$4,$5,$6)""",
                dst_id,
                r["name"],
                r["trigger_type"],
                r["trigger_value"],
                r["action_type"],
                r["action_value"],
            )
            counts["automation_rules"] += 1
        except Exception:
            log.debug(
                "clone_bot_settings: error copying automation_rule dst=%s",
                dst_id,
                exc_info=True,
            )
    return counts


async def get_weighted_routing_target(
    pool: asyncpg.Pool, cluster: str, exclude_bot_id: int
) -> dict | None:
    """Weighted random selection of routing target bot."""
    import random as _random

    raw_candidates = await pool.fetch(
        """SELECT m.bot_id, m.token, m.username, m.first_name,
                  COALESCE(bm.score,0) as score,
                  COALESCE(rw.weight,1.0) as weight
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           WHERE m.swarm_enabled=TRUE AND m.is_active=TRUE
             AND m.bot_role IN ('conversion','retention','general')
             AND m.cluster=$1 AND m.bot_id!=$2""",
        cluster,
        exclude_bot_id,
    )
    if not raw_candidates:
        return None
    candidates = _dec_bot_rows(raw_candidates)
    total = sum(float(c["weight"]) for c in candidates)
    if total <= 0:
        return candidates[0]
    r = _random.random() * total
    cum = 0.0
    for c in candidates:
        cum += float(c["weight"])
        if r <= cum:
            return c
    return candidates[-1]


# ── Telegram user accounts ──────────────────────────────────────────────────


async def get_tg_accounts(
    pool: asyncpg.Pool, owner_id: int, status_filter: str | None = None
) -> list:
    base = (
        "SELECT id, phone, tg_user_id, first_name, username, added_at, is_active, "
        "session_str, "
        "session_str AS session_string, "
        "COALESCE(acc_status, 'active') AS acc_status, status_checked_at, status_reason, "
        "(session_str IS NOT NULL AND session_str <> '') AS has_session, "
        "trust_score, cooldown_until, pool, tags, warnings "
        "FROM tg_accounts WHERE owner_id=$1"
    )
    if status_filter and status_filter != "all":
        return await pool.fetch(
            base + " AND COALESCE(acc_status,'active')=$2 ORDER BY added_at DESC",
            owner_id,
            status_filter,
        )
    return await pool.fetch(base + " ORDER BY added_at DESC", owner_id)


async def update_acc_status(
    pool: asyncpg.Pool, acc_id: int, status: str, reason: str = ""
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET acc_status=$1, status_reason=$2, status_checked_at=now() WHERE id=$3",
        status,
        reason,
        acc_id,
    )


async def get_tg_account(pool: asyncpg.Pool, acc_id: int, owner_id: int):
    return await pool.fetchrow(
        """SELECT a.*, p.proxy_url, p.label AS proxy_label
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.id=$1 AND a.owner_id=$2""",
        acc_id,
        owner_id,
    )


async def set_cf_credentials(pool, owner_id: int, api_token: str | None,
                             account_id: str, subdomain: str) -> None:
    """Сохранить доступы Cloudflare владельца в platform_users.settings_json.cf.
    Токен шифруется (token_vault). Если api_token пустой — НЕ перезатираем
    существующий (можно менять только account_id/subdomain). Merge, не clobber."""
    import json as _json
    from services.token_vault import encrypt_token
    # settings_json хранится как JSON-СТРОКА (text-колонка) — мержим в Python и
    # пишем строкой, как остальной код (mini_app_api settings save). JSONB-операторы
    # (||, ->, ::jsonb) тут падали «COALESCE types text and jsonb cannot be matched».
    raw = await pool.fetchval(
        "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    try:
        settings = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    cf = settings.get("cf") if isinstance(settings.get("cf"), dict) else {}
    cf["account_id"] = (account_id or "").strip()
    cf["subdomain"] = (subdomain or "").strip().replace(".workers.dev", "")
    if api_token and api_token.strip():
        cf["token_enc"] = encrypt_token(api_token.strip())
    settings["cf"] = cf
    await pool.execute(
        "UPDATE platform_users SET settings_json=$2 WHERE user_id=$1",
        owner_id, _json.dumps(settings))


async def set_ipv6_subnet(pool, owner_id: int, subnet: str) -> str:
    """Сохранить IPv6-подсеть владельца (settings_json.ipv6_subnet) для уникального
    IP на аккаунт без прокси. '' = выключить. Валидирует, что это IPv6-сеть.
    Возвращает нормализованную подсеть. Обновляет и in-memory кэш account_manager."""
    import json as _json
    subnet = (subnet or "").strip()
    if subnet:
        import ipaddress
        net = ipaddress.ip_network(subnet, strict=False)  # бросит при неверном
        if net.version != 6:
            raise ValueError("Нужна IPv6-подсеть (напр. 2a01:4f8:abcd::/64)")
        if net.num_addresses <= 2:
            raise ValueError("Подсеть слишком мала — нужна хотя бы /126, а лучше /64")
        subnet = str(net)
    raw = await pool.fetchval(
        "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    try:
        settings = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings["ipv6_subnet"] = subnet
    await pool.execute(
        "UPDATE platform_users SET settings_json=$2 WHERE user_id=$1",
        owner_id, _json.dumps(settings))
    try:
        from services import account_manager
        account_manager.set_owner_ipv6_subnet(owner_id, subnet)
    except Exception:
        pass
    return subnet


async def get_ipv6_subnet_or_raise(pool, owner_id: int | None) -> str:
    """IPv6-подсеть владельца ('' — не задана). Сбой чтения ПРОБРАСЫВАЕТ.

    Нужна там, где «не задана» и «не смогли прочитать» — разные вещи. Транспорт
    аккаунта именно такой случай: приняв сбой БД за «подсети нет», процесс уводит
    аккаунт напрямую с host-IP, тогда как другой процесс ходит с его IPv6. Одна
    сессия с двух адресов — AUTH_KEY_DUPLICATED, то есть мёртвый аккаунт.
    """
    if not owner_id:
        return ""
    import json as _json
    raw = await pool.fetchval(
        "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    if not raw:
        return ""
    try:
        data = raw if isinstance(raw, dict) else _json.loads(raw)
    except Exception:
        return ""      # битый JSON — это «не задана», а не сбой чтения
    return ((data or {}).get("ipv6_subnet") or "").strip()


async def get_ipv6_subnet(pool, owner_id: int | None) -> str:
    """IPv6-подсеть владельца из settings_json (или '' если не задана).

    Мягкий вариант: сбой чтения тоже даёт ''. Для решений о транспорте берите
    `get_ipv6_subnet_or_raise` — там разница принципиальна.
    """
    try:
        return await get_ipv6_subnet_or_raise(pool, owner_id)
    except Exception:
        return ""


async def get_cf_credentials(pool, owner_id: int) -> dict | None:
    """Достать доступы Cloudflare владельца (токен расшифрован). None если нет строки."""
    import json as _json
    from services.token_vault import decrypt_token
    try:
        raw = await pool.fetchval(
            "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    except Exception:
        return None
    if not raw:
        return None
    d = raw if isinstance(raw, dict) else _json.loads(raw)
    cf = (d or {}).get("cf") or {}
    tok_enc = cf.get("token_enc")
    return {
        "api_token": decrypt_token(tok_enc) if tok_enc else "",
        "account_id": cf.get("account_id") or "",
        "subdomain": cf.get("subdomain") or "",
        "has_token": bool(tok_enc),
    }


async def get_proxy_policy(pool, owner_id: int | None) -> str:
    """Политика прокси владельца: 'strict' (все соединения только через прокси)
    | 'allow_direct' (можно без прокси, риск блокировок). Хранится в
    platform_users.settings_json.proxy_policy; по умолчанию 'allow_direct'."""
    from services.proxy_policy import normalize_policy
    if not owner_id:
        return normalize_policy(None)
    try:
        raw = await pool.fetchval(
            "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    except Exception:
        return normalize_policy(None)
    if not raw:
        return normalize_policy(None)
    try:
        import json as _json
        data = _json.loads(raw) if isinstance(raw, str) else raw
        return normalize_policy((data or {}).get("proxy_policy"))
    except Exception:
        return normalize_policy(None)


# ── ОДИН источник полей для построения Telethon-клиента ──────────────────────
# Транспорт аккаунта (прокси / CF-релей / IPv6 / прямой выход) выбирает
# `account_manager._make_client` ПО ПОЛЯМ переданного словаря. Если выборка не
# содержит хотя бы одно из них, клиент пойдёт ДРУГИМ путём, чем остальные
# подсистемы того же аккаунта. Одна сессия с двух адресов — это
# AUTH_KEY_DUPLICATED, и Telegram отзывает ключ.
#
# Так и случилось: обе проверки здоровья писали свои SELECT-ы вручную и не
# выбирали cf_relay_url. Аккаунты с назначенным релеем ходили в операциях через
# edge-IP, а в проверке — напрямую с host-IP, и КАЖДАЯ проверка помечала их
# конфликтом. Поэтому список полей теперь один на всех.
TELETHON_ACC_COLS = (
    "a.id, a.owner_id, a.session_str, a.phone, a.first_name, a.username, "
    "a.acc_status, a.device_model, a.system_version, a.app_version, "
    "a.lang_code, a.system_lang_code, a.cf_relay_url, a.proxy_id, "
    "p.proxy_url, p.geo_country"
)
TELETHON_ACC_FROM = (
    "FROM tg_accounts a "
    "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE"
)


def telethon_accounts_query(where: str, extra_cols: str = "") -> str:
    """SELECT для аккаунтов, из которых будет построен Telethon-клиент.

    `where` — условия без слова WHERE. `extra_cols` — дополнительные колонки
    (через запятую, с префиксом `a.`), если экрану нужно что-то сверх транспорта.
    """
    cols = TELETHON_ACC_COLS + (", " + extra_cols if extra_cols else "")
    return f"SELECT {cols} {TELETHON_ACC_FROM} WHERE {where}"


async def get_account_for_telethon(pool, acc_id: int, owner_id: int | None = None):
    """Fetch account dict with device fingerprint + proxy_url + owner proxy_policy
    for _make_client. Возвращает dict (не Record), чтобы можно было доложить
    proxy_policy — вычисляемое поле, которого нет в таблице."""
    if owner_id is not None:
        row = await pool.fetchrow(
            """SELECT a.id, a.owner_id, a.session_str, a.phone, a.first_name,
                      a.device_model, a.system_version, a.app_version,
                      a.lang_code, a.system_lang_code, a.cf_relay_url,
                      a.proxy_id, p.proxy_url, p.geo_country
               FROM tg_accounts a
               LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
               WHERE a.id=$1 AND a.owner_id=$2""",
            acc_id,
            owner_id,
        )
    else:
        row = await pool.fetchrow(
            """SELECT a.id, a.owner_id, a.session_str, a.phone, a.first_name,
                      a.device_model, a.system_version, a.app_version,
                      a.lang_code, a.system_lang_code, a.cf_relay_url,
                      a.proxy_id, p.proxy_url, p.geo_country
               FROM tg_accounts a
               LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
               WHERE a.id=$1""",
            acc_id,
        )
    if row is None:
        return None
    d = dict(row)
    d["proxy_policy"] = await get_proxy_policy(pool, d.get("owner_id"))
    # Пер-владелец IPv6-подсеть (уникальный IP на аккаунт без прокси) — в _make_client.
    d["ipv6_subnet"] = await get_ipv6_subnet(pool, d.get("owner_id"))
    return d


async def add_tg_account(
    pool: asyncpg.Pool,
    owner_id: int,
    phone: str,
    session_str: str,
    tg_user_id: int,
    first_name: str,
    username: str,
    device_model: str | None = None,
    system_version: str | None = None,
    app_version: str | None = None,
    lang_code: str | None = None,
    system_lang_code: str | None = None,
) -> int:
    # Шифруем сессию at-rest (session_str — эквивалент пароля к аккаунту).
    # session_fp — детерминированный fingerprint для дедупа (шифр недетерминирован).
    from services.token_vault import encrypt_token, session_fingerprint

    _enc_session = encrypt_token(session_str)
    _fp = session_fingerprint(session_str)
    row = await pool.fetchrow(
        """INSERT INTO tg_accounts(owner_id, phone, session_str, tg_user_id,
               first_name, username, device_model, system_version, app_version,
               lang_code, system_lang_code, session_fp)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
           ON CONFLICT (owner_id, phone) DO UPDATE
           SET session_str=$3, tg_user_id=$4, first_name=$5, username=$6,
               device_model=COALESCE($7, tg_accounts.device_model),
               system_version=COALESCE($8, tg_accounts.system_version),
               app_version=COALESCE($9, tg_accounts.app_version),
               lang_code=COALESCE($10, tg_accounts.lang_code),
               system_lang_code=COALESCE($11, tg_accounts.system_lang_code),
               session_fp=$12,
               acc_status='active',
               status_reason=NULL,
               status_checked_at=now(),
               is_active=true,
               -- Переимпорт = НОВАЯ рабочая сессия: снимаем ВСЕ операционные
               -- блокировки, унаследованные от прошлой (сожжённой) сессии, иначе
               -- «переподключил, а флот не работает» — аккаунт молча исключался
               -- из операций/мониторов:
               --  • in_operation — залипший lock исключал из выборки/прогрева;
               --  • cooldown_until — старый кулдаун держал respect_cooldown-фильтр;
               --  • trust_score — крит-обнуление (trust_engine) не проходило
               --    min_trust-гейт операций и показывало 0% здоровья.
               in_operation=FALSE,
               cooldown_until=NULL,
               trust_score=GREATEST(COALESCE(tg_accounts.trust_score, 1.0), 1.0),
               flood_count_7d=0,
               last_used=now()
           RETURNING id""",
        owner_id,
        phone,
        _enc_session,
        tg_user_id,
        first_name,
        username,
        device_model,
        system_version,
        app_version,
        lang_code,
        system_lang_code,
        _fp,
    )
    acc_id = row["id"]
    # Регистрируем связь телефон→владелец для анти-абуз системы
    await record_phone_link(pool, phone, owner_id)
    # CF-релей НЕ раздаём автоматически: транспорт — прокси (задаёт пользователь)
    # или прямой host-IP. CF/IPv6 — только по явному выбору пользователя. Авто-
    # раздача CF делала его «основой» и запирала аккаунты на нерабочих воркерах.
    return acc_id


async def remove_tg_account(pool: asyncpg.Pool, acc_id: int, owner_id: int) -> bool:
    # managed_channels.acc_id не имеет FK ON DELETE CASCADE — чистим вручную в
    # одной транзакции, иначе каналы удалённого аккаунта остаются «фантомами»
    # и продолжают засчитываться в лимит каналов.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM managed_channels WHERE owner_id=$1 AND acc_id=$2",
                owner_id, acc_id,
            )
            result = await conn.execute(
                "DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id, owner_id,
            )
    return result != "DELETE 0"


async def update_tg_account_used(pool: asyncpg.Pool, acc_id: int, owner_id: int) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET last_used=now() WHERE id=$1 AND owner_id=$2",
        acc_id,
        owner_id,
    )


async def update_tg_account_status(
    pool: asyncpg.Pool,
    acc_id: int,
    owner_id: int,
    is_active: bool,
) -> bool:
    """Обновляет статус активности аккаунта. Возвращает True если запись найдена и обновлена."""
    result = await pool.execute(
        "UPDATE tg_accounts SET is_active=$3 WHERE id=$1 AND owner_id=$2",
        acc_id,
        owner_id,
        is_active,
    )
    return result != "UPDATE 0"


async def get_active_account_for_owner(
    pool: asyncpg.Pool, owner_id: int
) -> dict | None:
    """Возвращает первый активный аккаунт пользователя (используется ranking_checker'ом).

    Всегда фильтруется по owner_id — пользователь видит только свои аккаунты.
    """
    row = await pool.fetchrow(
        "SELECT a.*, p.proxy_url FROM tg_accounts a "
        "LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL "
        "ORDER BY a.last_used DESC NULLS LAST, a.added_at DESC LIMIT 1",
        owner_id,
    )
    return dict(row) if row else None


# ── Search rankings ─────────────────────────────────────────────────────────


async def get_tracked_keywords(pool: asyncpg.Pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT id, keyword, is_active, created_at FROM tracked_keywords "
        "WHERE bot_id=$1 ORDER BY created_at",
        bot_id,
    )


async def add_tracked_keyword(
    pool: asyncpg.Pool, bot_id: int, owner_id: int, keyword: str
) -> bool:
    try:
        await pool.execute(
            "INSERT INTO tracked_keywords(bot_id, owner_id, keyword) VALUES($1,$2,$3)",
            bot_id,
            owner_id,
            keyword,
        )
        return True
    except Exception:
        return False


async def remove_tracked_keyword(
    pool: asyncpg.Pool, keyword_id: int, owner_id: int
) -> bool:
    result = await pool.execute(
        "DELETE FROM tracked_keywords WHERE id=$1 AND owner_id=$2",
        keyword_id,
        owner_id,
    )
    return result != "DELETE 0"


async def get_keyword_rankings(
    pool: asyncpg.Pool, keyword_id: int, limit: int = 10, owner_id: int | None = None
) -> list:
    if owner_id is not None:
        return await pool.fetch(
            "SELECT sr.position, sr.checked_at FROM search_rankings sr "
            "JOIN tracked_keywords tk ON tk.id=sr.keyword_id AND tk.owner_id=$3 "
            "WHERE sr.keyword_id=$1 ORDER BY sr.checked_at DESC LIMIT $2",
            keyword_id,
            limit,
            owner_id,
        )
    return await pool.fetch(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT $2",
        keyword_id,
        limit,
    )


async def get_latest_ranking(
    pool: asyncpg.Pool, keyword_id: int, owner_id: int | None = None
):
    if owner_id is not None:
        return await pool.fetchrow(
            "SELECT sr.position, sr.checked_at FROM search_rankings sr "
            "JOIN tracked_keywords tk ON tk.id=sr.keyword_id AND tk.owner_id=$2 "
            "WHERE sr.keyword_id=$1 ORDER BY sr.checked_at DESC LIMIT 1",
            keyword_id,
            owner_id,
        )
    return await pool.fetchrow(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT 1",
        keyword_id,
    )


async def save_ranking(
    pool: asyncpg.Pool, keyword_id: int, bot_id: int, position
) -> None:
    await pool.execute(
        "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
        keyword_id,
        bot_id,
        position,
    )


async def get_ranking_history(
    pool: asyncpg.Pool, keyword_id: int, limit: int = 7, owner_id: int | None = None
) -> list:
    """Return last N ranking records for a keyword: [(position, checked_at)]."""
    if owner_id is not None:
        return await pool.fetch(
            "SELECT sr.position, sr.checked_at FROM search_rankings sr "
            "JOIN tracked_keywords tk ON tk.id=sr.keyword_id AND tk.owner_id=$3 "
            "WHERE sr.keyword_id=$1 ORDER BY sr.checked_at DESC LIMIT $2",
            keyword_id,
            limit,
            owner_id,
        )
    return await pool.fetch(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT $2",
        keyword_id,
        limit,
    )


async def get_all_keywords_with_latest_ranking(
    pool: asyncpg.Pool, owner_id: int
) -> list[dict]:
    """Возвращает все ключевые слова пользователя с последней позицией и username бота.

    Формат: [{"keyword_id", "keyword", "bot_id", "bot_username", "position", "checked_at"}]
    """
    rows = await pool.fetch(
        """SELECT
               tk.id              AS keyword_id,
               tk.keyword         AS keyword,
               tk.bot_id          AS bot_id,
               mb.username        AS bot_username,
               sr.position        AS position,
               sr.checked_at      AS checked_at
           FROM tracked_keywords tk
           JOIN managed_bots mb ON mb.bot_id = tk.bot_id
           LEFT JOIN LATERAL (
               SELECT position, checked_at
               FROM search_rankings
               WHERE keyword_id = tk.id
               ORDER BY checked_at DESC
               LIMIT 1
           ) sr ON TRUE
           WHERE tk.owner_id = $1
           ORDER BY mb.username, tk.keyword""",
        owner_id,
    )
    return [
        {
            "keyword_id": r["keyword_id"],
            "keyword": r["keyword"],
            "bot_id": r["bot_id"],
            "bot_username": r["bot_username"],
            "position": r["position"],
            "checked_at": r["checked_at"],
        }
        for r in rows
    ]


async def get_bot_owner(pool: asyncpg.Pool, bot_id: int) -> int | None:
    """Возвращает added_by (owner_id) для бота или None если бот не найден."""
    return await pool.fetchval(
        "SELECT added_by FROM managed_bots WHERE bot_id=$1 AND is_active=TRUE",
        bot_id,
    )


async def toggle_keyword_active(
    pool: asyncpg.Pool, keyword_id: int, owner_id: int
) -> bool | None:
    """Переключает is_active ключевого слова. Возвращает новое значение или None если не найдено."""
    row = await pool.fetchrow(
        """UPDATE tracked_keywords
           SET is_active = NOT is_active
           WHERE id = $1 AND owner_id = $2
           RETURNING is_active""",
        keyword_id,
        owner_id,
    )
    return row["is_active"] if row else None


async def toggle_keyword_notify(
    pool: asyncpg.Pool, bot_id: int, owner_id: int
) -> bool | None:
    """Переключает notify_enabled для всех ключевых слов бота.

    Если хотя бы одно включено — выключает все; иначе — включает все.
    Возвращает новое значение или None если ключевых слов не найдено.
    """
    current = await pool.fetchval(
        "SELECT bool_or(notify_enabled) FROM tracked_keywords WHERE bot_id=$1 AND owner_id=$2",
        bot_id,
        owner_id,
    )
    if current is None:
        return None
    new_value = not current
    await pool.execute(
        "UPDATE tracked_keywords SET notify_enabled=$3 WHERE bot_id=$1 AND owner_id=$2",
        bot_id,
        owner_id,
        new_value,
    )
    return new_value


async def get_keyword_notify_enabled(
    pool: asyncpg.Pool, bot_id: int, owner_id: int
) -> bool:
    """Возвращает True если хотя бы одно ключевое слово бота имеет notify_enabled=TRUE."""
    val = await pool.fetchval(
        "SELECT bool_or(notify_enabled) FROM tracked_keywords WHERE bot_id=$1 AND owner_id=$2",
        bot_id,
        owner_id,
    )
    return bool(val)


# Один INSERT на обе двери записи каналов: и точечное добавление, и полный
# ре-импорт. members_count не даём обнулять: частичная страница диалогов или
# временный сбой Telegram не обязаны откатывать известное число участников
# назад (из-за этого в мини-аппе у ВСЕХ каналов показывался 0).
_MANAGED_CHANNEL_UPSERT_SQL = """
    INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username,
                                 access_hash, type, is_admin, is_creator, members_count)
    VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (owner_id, channel_id) DO UPDATE
    SET title=EXCLUDED.title, username=EXCLUDED.username,
        acc_id=EXCLUDED.acc_id, access_hash=EXCLUDED.access_hash,
        type=EXCLUDED.type,
        is_admin=COALESCE(EXCLUDED.is_admin, managed_channels.is_admin),
        is_creator=COALESCE(EXCLUDED.is_creator, managed_channels.is_creator),
        members_count=CASE WHEN EXCLUDED.members_count > 0
                            THEN EXCLUDED.members_count
                            ELSE managed_channels.members_count END
"""


def _managed_channel_rows(owner_id: int, acc_id: int, channels: list[dict]) -> list[tuple]:
    """Строки под _MANAGED_CHANNEL_UPSERT_SQL."""
    return [
        (
            owner_id,
            acc_id,
            ch["id"],
            ch.get("title", ""),
            ch.get("username", ""),
            ch.get("access_hash", 0),
            ch.get("type", "channel"),
            ch.get("is_admin"),
            ch.get("is_creator"),
            int(ch.get("members") or 0),
        )
        for ch in channels
    ]


async def upsert_managed_channels(
    pool: asyncpg.Pool, owner_id: int, acc_id: int, channels: list[dict],
    *, complete: bool,
) -> int:
    """Сверяет список каналов аккаунта с базой. Возвращает кол-во переданных строк.

    `complete` — обязательный и осознанный ответ на вопрос «переданный список
    точно исчерпывающий?». Это единственное, что отличает ре-импорт от потери
    данных:

    * `complete=True` — строки этого аккаунта, которых нет в списке, удаляются
      (канал действительно отдан/удалён). Уцелевшие строки НЕ пересоздаются, а
      обновляются: раньше здесь был DELETE всех строк и INSERT заново, и вместе
      со строками обнулялось число участников — те самые «0 участников» у всех
      каналов, которые чинили в add_managed_channels, но не тут.
    * `complete=False` — не удаляем ничего, просто добавляем/обновляем
      переданное. Так ведёт себя любой срез: скан диалогов упирается в лимит
      (account_manager.scan_owned_assets → truncated), страница пагинации,
      один только что созданный канал. По неполному списку отсутствие канала
      НИЧЕГО не доказывает, а удаление его строки — потеря данных, которую
      никто не заметит до следующей рассылки.

    Параметр именованный и без значения по умолчанию намеренно: забыть его
    нельзя, а значит нельзя и случайно получить удаление.
    """
    if not channels:
        return 0
    if not complete:
        log.warning(
            "upsert_managed_channels: список неполный (acc_id=%s) — ничего не "
            "удаляем, только добавляем %d строк", acc_id, len(channels),
        )
        return await add_managed_channels(pool, owner_id, acc_id, channels)

    ids = []
    for ch in channels:
        try:
            ids.append(int(ch["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    rows = _managed_channel_rows(owner_id, acc_id, channels)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM managed_channels "
                " WHERE owner_id=$1 AND acc_id=$2 "
                "   AND NOT (channel_id = ANY($3::bigint[]))",
                owner_id, acc_id, ids,
            )
            await conn.executemany(_MANAGED_CHANNEL_UPSERT_SQL, rows)
    return len(channels)


async def add_managed_channels(
    pool: asyncpg.Pool, owner_id: int, acc_id: int, channels: list[dict]
) -> int:
    """Точечно добавляет/обновляет каналы в managed_channels БЕЗ удаления
    остальных ранее сохранённых строк аккаунта.

    Используй для ЧАСТИЧНЫХ данных (одна страница пагинации диалогов, один
    только что созданный канал, срез списка, обрезанный лимитом квоты
    подписки) — то есть везде, где переданный `channels` не гарантированно
    является полным набором каналов аккаунта. Для настоящего полного
    ре-импорта (когда список заведомо полный и стейл-записи действительно
    нужно удалить) — см. upsert_managed_channels(..., complete=True).
    """
    if not channels:
        return 0
    await pool.executemany(
        _MANAGED_CHANNEL_UPSERT_SQL,
        _managed_channel_rows(owner_id, acc_id, channels),
    )
    return len(channels)


async def finalize_stale_parser_runs(pool: asyncpg.Pool, older_than_minutes: int = 60) -> int:
    """Помечает зависшие parser_runs (pending/running дольше N минут) как failed.

    Возвращает число обновлённых строк. Устраняет сироты, оставшиеся от сбоя
    парсера (например, таймаут client.connect()) до фикса финализации.
    """
    result = await pool.execute(
        """UPDATE parser_runs
           SET status='failed',
               error=COALESCE(error, 'Прервано: процесс завершился до финализации'),
               finished_at=now()
           WHERE status IN ('pending', 'running')
             AND started_at < now() - ($1::int * interval '1 minute')""",
        older_than_minutes,
    )
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0


async def get_managed_channels(
    pool: asyncpg.Pool, owner_id: int, acc_id: int | None = None
) -> list[asyncpg.Record]:
    """Возвращает каналы из кэша. Если acc_id задан — только для этого аккаунта."""
    if acc_id is not None:
        return await pool.fetch(
            "SELECT * FROM managed_channels WHERE owner_id=$1 AND acc_id=$2 ORDER BY title",
            owner_id,
            acc_id,
        )
    return await pool.fetch(
        "SELECT * FROM managed_channels WHERE owner_id=$1 ORDER BY acc_id, title",
        owner_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PLATFORM REFERRAL SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

import random
import string as _string


def _gen_code() -> str:
    chars = _string.ascii_uppercase + _string.digits
    return "inv_" + "".join(random.choices(chars, k=6))


async def get_or_create_referral_code(pool: asyncpg.Pool, user_id: int) -> str:
    """Return existing referral code or create a new unique one."""
    existing = await pool.fetchval(
        "SELECT code FROM platform_referral_codes WHERE user_id=$1", user_id
    )
    if existing:
        return existing
    for _ in range(10):
        code = _gen_code()
        try:
            await pool.execute(
                "INSERT INTO platform_referral_codes(user_id, code) VALUES($1,$2)",
                user_id,
                code,
            )
            return code
        except asyncpg.UniqueViolationError:
            continue
        except Exception:
            continue
    raise RuntimeError("Failed to generate unique referral code")


async def get_user_by_referral_code(pool: asyncpg.Pool, code: str) -> int | None:
    """Return referrer user_id for a given code, or None."""
    return await pool.fetchval(
        "SELECT user_id FROM platform_referral_codes WHERE code=$1", code
    )


async def record_platform_referral(
    pool: asyncpg.Pool, referrer_id: int, referred_id: int
) -> bool:
    """Record a new platform referral. Returns False if already exists or invalid."""
    if referrer_id == referred_id:
        return False
    # Anti-fraud: max 100 referrals per month
    monthly = (
        await pool.fetchval(
            """SELECT COUNT(*) FROM platform_referrals
           WHERE referrer_id=$1 AND created_at >= now() - INTERVAL '30 days'""",
            referrer_id,
        )
        or 0
    )
    if monthly >= 100:
        return False
    try:
        await pool.execute(
            "INSERT INTO platform_referrals(referrer_id, referred_id) VALUES($1,$2)",
            referrer_id,
            referred_id,
        )
        await pool.execute(
            "UPDATE platform_referral_codes SET total_clicks=total_clicks+1 WHERE user_id=$1",
            referrer_id,
        )
        return True
    except Exception:
        return False


async def mark_referral_activated(pool: asyncpg.Pool, referred_id: int) -> int | None:
    """Mark referral as activated (user created first bot). Returns referrer_id or None."""
    row = await pool.fetchrow(
        """UPDATE platform_referrals
           SET activated_at=now()
           WHERE referred_id=$1 AND activated_at IS NULL
           RETURNING referrer_id""",
        referred_id,
    )
    return row["referrer_id"] if row else None


async def mark_referral_paid(pool: asyncpg.Pool, referred_id: int) -> int | None:
    """Mark referral as paid (referred user confirmed payment). Returns referrer_id or None."""
    row = await pool.fetchrow(
        """UPDATE platform_referrals
           SET paid_at=now()
           WHERE referred_id=$1 AND paid_at IS NULL
           RETURNING referrer_id""",
        referred_id,
    )
    return row["referrer_id"] if row else None


async def get_referral_stats(pool: asyncpg.Pool, user_id: int) -> dict:
    """Return referral dashboard data for a user."""
    code = await get_or_create_referral_code(pool, user_id)
    total = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", user_id
        )
        or 0
    )
    active = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1 AND activated_at IS NOT NULL",
            user_id,
        )
        or 0
    )
    paid = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1 AND paid_at IS NOT NULL",
            user_id,
        )
        or 0
    )
    rewards = await pool.fetch(
        "SELECT level, plan, days, given_at FROM referral_rewards WHERE user_id=$1 ORDER BY given_at",
        user_id,
    )
    return {
        "code": code,
        "total": total,
        "active": active,
        "paid": paid,
        "rewards": [dict(r) for r in rewards],
    }


async def get_referral_leaderboard_platform(
    pool: asyncpg.Pool, limit: int = 10
) -> list[dict]:
    """Top referrers by number of paying referrals."""
    rows = await pool.fetch(
        """SELECT pr.referrer_id, pu.first_name, pu.username,
                  COUNT(*) FILTER (WHERE pr.paid_at IS NOT NULL) AS paid_count,
                  COUNT(*) AS total_count
           FROM platform_referrals pr
           JOIN platform_users pu ON pu.user_id = pr.referrer_id
           GROUP BY pr.referrer_id, pu.first_name, pu.username
           HAVING COUNT(*) FILTER (WHERE pr.paid_at IS NOT NULL) > 0
           ORDER BY paid_count DESC, total_count DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def _sync_platform_user_plan(pool, user_id: int, sub_row) -> None:
    """Отразить активную строку subscriptions в денормализованном platform_users
    и сбросить кеш плана.

    Источник правды — таблица subscriptions (её читают get_plan и бот). Но
    platform_users.current_plan/plan_expires_at — второе хранилище, которое
    читают админ-списки и часть мини-аппа. Любой писатель subscriptions ОБЯЗАН
    синхронно обновить platform_users, иначе подписку видно в боте, но не в
    мини-аппе (классический рассинхрон). sub_row — результат RETURNING
    (plan, expires_at) апсерта subscriptions.
    """
    if not sub_row:
        return
    try:
        await pool.execute(
            "UPDATE platform_users SET current_plan=$2, plan_expires_at=$3 WHERE user_id=$1",
            user_id,
            sub_row["plan"],
            sub_row["expires_at"],
        )
    except Exception:
        log_exc_swallow(log, "_sync_platform_user_plan: platform_users sync failed", user_id=user_id)
    _invalidate_plan_cache(user_id)


def _invalidate_plan_cache(user_id: int) -> None:
    """Сброс in-process кеша плана (единый на весь рантайм — один процесс).

    Ленивый импорт, чтобы избежать циклической зависимости db ↔ bot.utils.
    """
    try:
        from bot.utils.subscription import invalidate_plan_cache
        invalidate_plan_cache(user_id)
    except Exception:
        log_exc_swallow(log, "_invalidate_plan_cache failed", user_id=user_id)


# Reward tier definitions
_REWARD_TIERS = [
    ("basic", "active", 5, "starter", 14),
    ("silver", "paid", 3, "starter", 30),
    ("gold", "paid", 10, "pro", 30),
    ("platinum", "paid", 25, "enterprise", 30),
]


async def check_and_grant_rewards(
    pool: asyncpg.Pool, referrer_id: int, bot
) -> list[str]:
    """Check thresholds and grant any unclaimed rewards. Returns list of newly granted levels."""
    stats = await get_referral_stats(pool, referrer_id)
    granted = []
    existing_levels = {r["level"] for r in stats["rewards"]}

    for level, metric, threshold, plan, days in _REWARD_TIERS:
        if level in existing_levels:
            continue
        count = stats["active"] if metric == "active" else stats["paid"]
        if count < threshold:
            continue
        try:
            await pool.execute(
                "INSERT INTO referral_rewards(user_id, level, plan, days) VALUES($1,$2,$3,$4)",
                referrer_id,
                level,
                plan,
                days,
            )
        except Exception:
            continue
        # Extend subscription. RETURNING даёт итоговый plan/expires_at, чтобы
        # синхронно обновить platform_users (второе хранилище) — иначе бот видит
        # подписку (get_plan читает subscriptions), а мини-апп/админка,
        # читающие platform_users.current_plan, показывают free.
        sub_row = await pool.fetchrow(
            """INSERT INTO subscriptions (user_id, plan, expires_at, is_active)
               VALUES ($1, $2, now() + ($3 || ' days')::INTERVAL, true)
               ON CONFLICT (user_id) DO UPDATE SET
                   plan = CASE
                       WHEN ARRAY_POSITION(ARRAY['starter','pro','enterprise'], EXCLUDED.plan) >
                            ARRAY_POSITION(ARRAY['starter','pro','enterprise'], subscriptions.plan)
                       THEN EXCLUDED.plan ELSE subscriptions.plan END,
                   is_active  = true,
                   expires_at = GREATEST(subscriptions.expires_at, now())
                              + ($3 || ' days')::INTERVAL
               RETURNING plan, expires_at""",
            referrer_id,
            plan,
            str(days),
        )
        await _sync_platform_user_plan(pool, referrer_id, sub_row)
        granted.append(level)
        # Notify referrer
        level_names = {
            "basic": "🥉 Базовый",
            "silver": "🥈 Серебро",
            "gold": "🥇 Золото",
            "platinum": "💎 Платина",
        }
        plan_names = {"starter": "Starter", "pro": "Pro", "enterprise": "Enterprise"}
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Реферальная награда получена!</b>\n\n"
                f"Уровень: {level_names.get(level, level)}\n"
                f"Награда: <b>{days} дней {plan_names.get(plan, plan)} бесплатно!</b>\n\n"
                f"Продолжайте приглашать — впереди ещё уровни!\n"
                f"/referral — ваш прогресс",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(
                log,
                "Сбой уведомления о реферальной награде",
                referrer_id=referrer_id,
                level=level,
            )

    return granted


async def give_welcome_bonus(pool: asyncpg.Pool, referred_id: int, bot) -> bool:
    """Give 7 days Starter to a new user who joined via referral link. One-time only."""
    updated = await pool.fetchval(
        """UPDATE platform_referrals
           SET welcome_bonus_given=true
           WHERE referred_id=$1 AND welcome_bonus_given=false
           RETURNING id""",
        referred_id,
    )
    if not updated:
        return False
    sub_row = await pool.fetchrow(
        """INSERT INTO subscriptions (user_id, plan, expires_at, is_active)
           VALUES ($1, 'starter', now() + INTERVAL '7 days', true)
           ON CONFLICT (user_id) DO UPDATE SET
               plan       = CASE WHEN subscriptions.is_active THEN subscriptions.plan ELSE 'starter' END,
               is_active  = true,
               expires_at = GREATEST(subscriptions.expires_at, now()) + INTERVAL '7 days'
           RETURNING plan, expires_at""",
        referred_id,
    )
    # Синхронизируем platform_users, иначе welcome-бонус виден боту, но не
    # мини-аппу/админке (см. _sync_platform_user_plan).
    await _sync_platform_user_plan(pool, referred_id, sub_row)
    try:
        await bot.send_message(
            referred_id,
            "🎁 <b>Подарок от реферальной программы!</b>\n\n"
            "Вы пришли по реферальной ссылке и получаете <b>7 дней Starter бесплатно!</b>\n\n"
            "💡 Поделитесь своей ссылкой — и вы тоже можете получить бесплатный тариф:\n"
            "/referral",
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой уведомления о реферальном подарке", referred_id=referred_id
        )
    return True


async def deactivate_account(
    pool: asyncpg.Pool, account_id: int, reason: str = ""
) -> None:
    """Mark account as inactive (banned / PeerFlood). Called by operation handlers."""
    await pool.execute(
        "UPDATE tg_accounts SET is_active=false WHERE id=$1",
        account_id,
    )
    if reason:
        log.warning("Account %s deactivated: %s", account_id, reason)


# ── Trust Engine ───────────────────────────────────────────────────────────


async def get_trusted_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[asyncpg.Record]:
    """Return active accounts not in cooldown, ordered by trust_score DESC."""
    return await pool.fetch(
        """SELECT a.*, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.is_active=true AND a.session_str IS NOT NULL
             AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
           ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST""",
        owner_id,
    )


async def record_flood_event(
    pool: asyncpg.Pool,
    account_id: int,
    operation: str = "",
    flood_seconds: int = 0,
) -> None:
    """Record a flood event: increment counter, set cooldown, log it."""
    cooldown_hours = 2
    if flood_seconds > 3600:
        cooldown_hours = 6
    await pool.execute(
        """UPDATE tg_accounts
           SET flood_count_7d = flood_count_7d + 1,
               last_flood_at  = NOW(),
               cooldown_until = NOW() + ($1 * INTERVAL '1 hour')
           WHERE id = $2""",
        cooldown_hours,
        account_id,
    )
    await pool.execute(
        """INSERT INTO account_flood_log(account_id, operation, flood_seconds)
           VALUES($1, $2, $3)""",
        account_id,
        operation,
        flood_seconds,
    )


async def get_notification_settings(pool: asyncpg.Pool, user_id: int) -> dict:
    """Return notification preferences for a user. Defaults to all True if no record."""
    row = await pool.fetchrow(
        "SELECT new_user, flood_warning, position_change, op_complete, restriction "
        "FROM notification_settings WHERE user_id=$1",
        user_id,
    )
    if row:
        result = dict(row)
        result.setdefault("deploy", True)
        return result
    return {
        "new_user": True,
        "flood_warning": True,
        "position_change": True,
        "op_complete": True,
        "restriction": True,
        "deploy": True,
    }


# In-memory rate-limit cache: (user_id, pref) -> last_sent_timestamp
# Prevents notification spam when many events fire simultaneously.
_notify_cooldown: dict[tuple[int, str, str | None], float] = {}
_NOTIFY_COOLDOWN_SECONDS = 60  # minimum interval between same-type notifications per user
_NOTIFY_CACHE_MAX = 10000  # prevent unbounded memory growth


def _cleanup_notify_cooldown() -> None:
    """Evict stale entries when cache grows too large."""
    if len(_notify_cooldown) <= _NOTIFY_CACHE_MAX:
        return
    cutoff = time.monotonic() - _NOTIFY_COOLDOWN_SECONDS * 2
    stale = [k for k, v in _notify_cooldown.items() if v < cutoff]
    for k in stale[:len(stale) // 2]:
        _notify_cooldown.pop(k, None)


async def notify_dedup_ok(pool, user_id: int, key: str, cooldown_s: int) -> bool:
    """ПЕРСИСТЕНТНЫЙ анти-спам для повторяющихся уведомлений.

    True (и фиксирует момент), только если для (user_id, key) прошло ≥ cooldown_s
    с прошлого раза. В отличие от in-memory rate-limit в notify_if_enabled, это
    переживает РЕСТАРТ процесса — иначе один и тот же алерт (напр. «критический
    риск бана» по аккаунту) присылался заново после каждого перезапуска бота,
    заваливая владельца (жалоба: одинаковые уведомления каждые несколько минут).

    Атомарно через INSERT .. ON CONFLICT .. DO UPDATE .. WHERE .. RETURNING:
    первая вставка и «прошёл кулдаун» → есть строка (True); свежая запись в окне →
    WHERE не выполняется, 0 строк (False). Fail-open: любой сбой БД → True (не
    глушим потенциально важное из-за инфраструктурной ошибки)."""
    try:
        # Таблицу заводит миграция (schema_v184), а не этот вызов: DDL на каждое
        # уведомление — лишний рейс до базы и блокировка в горячем пути.
        row = await pool.fetchrow(
            "INSERT INTO notification_dedup(user_id, dedup_key, last_sent) "
            "VALUES($1, $2, NOW()) "
            "ON CONFLICT(user_id, dedup_key) DO UPDATE SET last_sent = NOW() "
            "  WHERE notification_dedup.last_sent < NOW() - ($3 * INTERVAL '1 second') "
            "RETURNING user_id",
            int(user_id), str(key), int(cooldown_s),
        )
        return row is not None
    except Exception as exc:
        log.debug("notify_dedup_ok fail-open (%s): %s", key, exc)
        return True


async def notify_if_enabled(
    pool: asyncpg.Pool,
    bot,
    user_id: int,
    pref: str,
    text: str,
    reply_markup=None,
    *,
    dedup_key: str | None = None,
) -> None:
    """Send a notification to user only if the given preference flag is True.

    Rate-limited: each (user_id, pref, dedup_key) triple can fire at most once
    per _NOTIFY_COOLDOWN_SECONDS to prevent spam when many events happen at once.

    dedup_key разделяет кулдаун для логически разных событий одного типа.
    Без него уведомления вроде "new_user" со всех ботов владельца делили один
    слот, и при потоке новых подписчиков большинство уведомлений терялось.
    Для таких событий передавайте уникальный ключ (например, id нового юзера).
    """
    try:
        # Rate-limit check (in-memory, process-scoped)
        _cleanup_notify_cooldown()
        now = time.monotonic()
        key = (user_id, pref, dedup_key)
        last_sent = _notify_cooldown.get(key, 0.0)
        if now - last_sent < _NOTIFY_COOLDOWN_SECONDS:
            return
        _notify_cooldown[key] = now

        settings = await get_notification_settings(pool, user_id)
        if not settings.get(pref, True):
            return
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=reply_markup
        )
    except Exception as exc:
        # Доставка уведомлений — критичный путь: сбой логируем на WARNING, а не
        # глушим в DEBUG. Частые причины: владелец не нажал /start у основного
        # бота (403) или заблокировал его — иначе это полностью невидимо в проде.
        log.warning(
            "notify_if_enabled: не доставлено user=%s pref=%s: %s",
            user_id, pref, str(exc)[:200],
        )


# ── Global Presence Factory ────────────────────────────────────────────────


async def create_global_presence_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    asset_type: str,
    name_pattern: str,
    username_pattern: str | None,
    geo_selection: dict,
    account_selection: dict,
    template_id: int | None = None,
    *,
    project_name: str | None = None,
    roles: list[str] | None = None,
    levels: list[str] | None = None,
    name_pool: list[str] | None = None,
    username_pool: list[str] | None = None,
    about_pool: list[str] | None = None,
    avatar_style: str | None = None,
    plan_seed: int | None = None,
) -> int:
    """Create a plan and return its id.

    Поля генератора (roles/levels/пулы/seed) необязательны: планы, собранные
    старым путём «один паттерн», продолжают работать без них.
    """

    def _j(value):
        return json.dumps(value, ensure_ascii=False) if value else None

    return await pool.fetchval(
        """INSERT INTO global_presence_plans
               (owner_id, asset_type, name_pattern, username_pattern,
                geo_selection, account_selection, template_id, status,
                project_name, roles, levels, name_pool, username_pool,
                about_pool, avatar_style, plan_seed)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,'queued',
                   $8,$9::jsonb,$10::jsonb,$11::jsonb,$12::jsonb,
                   $13::jsonb,$14,$15)
           RETURNING id""",
        owner_id,
        asset_type,
        name_pattern,
        username_pattern,
        json.dumps(geo_selection, ensure_ascii=False),
        json.dumps(account_selection, ensure_ascii=False),
        template_id,
        project_name,
        _j(roles),
        _j(levels),
        _j(name_pool),
        _j(username_pool),
        _j(about_pool),
        avatar_style,
        plan_seed,
    )


async def create_global_presence_targets(
    pool: asyncpg.Pool, plan_id: int, targets: list[dict]
) -> int:
    """Bulk-insert targets for a plan. Returns count inserted."""
    if not targets:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO global_presence_targets
                   (plan_id, country, country_code, region, city, city_slug,
                    language, timezone, asset_type, planned_name, planned_username,
                    selected_account_id, role, level, planned_about,
                    avatar_seed, avatar_style)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
            [
                (
                    plan_id,
                    t.get("country"),
                    t.get("country_code"),
                    t.get("region"),
                    t.get("city"),
                    t.get("city_slug"),
                    t.get("language"),
                    t.get("timezone"),
                    t.get("asset_type", "channel"),
                    t.get("planned_name"),
                    t.get("planned_username"),
                    t.get("selected_account_id"),
                    t.get("role"),
                    t.get("level"),
                    t.get("planned_about"),
                    t.get("avatar_seed"),
                    t.get("avatar_style"),
                )
                for t in targets
            ],
        )
    return len(targets)


async def get_taken_usernames(pool: asyncpg.Pool, owner_id: int, limit: int = 20000) -> set[str]:
    """Username, которые владелец уже занял или запланировал.

    Нужен аллокатору ДО запуска: без этого списка новый проект спокойно
    сгенерирует имя, уже стоящее на канале того же владельца, и цель упадёт
    в бою «username занят» — при том что коллизию было видно заранее.

    Fail-open: при ошибке возвращаем пустое множество. Потерять подсказку
    хуже, чем не дать запустить проект.
    """
    taken: set[str] = set()
    try:
        rows = await pool.fetch(
            """SELECT username AS u FROM managed_channels
                   WHERE owner_id=$1 AND username IS NOT NULL AND username<>''
               UNION
               SELECT COALESCE(t.final_username, t.planned_username) AS u
                   FROM global_presence_targets t
                   JOIN global_presence_plans p ON p.id = t.plan_id
                   WHERE p.owner_id=$1
                     AND COALESCE(t.final_username, t.planned_username) IS NOT NULL
               LIMIT $2""",
            owner_id,
            limit,
        )
        for r in rows:
            val = (r["u"] or "").strip().lstrip("@").lower()
            if val:
                taken.add(val)
    except Exception:
        log.warning("get_taken_usernames failed for owner=%s", owner_id, exc_info=True)
    return taken


async def get_global_presence_plan(
    pool: asyncpg.Pool, plan_id: int, owner_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )


async def get_global_presence_plans(
    pool: asyncpg.Pool, owner_id: int, limit: int = 10, offset: int = 0
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM global_presence_plans WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        owner_id,
        limit,
        offset,
    )


async def get_global_presence_stats(
    pool: asyncpg.Pool, plan_id: int, owner_id: int | None = None
) -> dict:
    if owner_id is not None:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE gpt.status='pending')  AS pending,
                   COUNT(*) FILTER (WHERE gpt.status='done')     AS done,
                   COUNT(*) FILTER (WHERE gpt.status='failed')   AS failed,
                   COUNT(*) FILTER (WHERE gpt.status='running')  AS running,
                   COUNT(*) AS total
               FROM global_presence_targets gpt
               JOIN global_presence_plans gpp ON gpp.id=gpt.plan_id AND gpp.owner_id=$2
               WHERE gpt.plan_id=$1""",
            plan_id,
            owner_id,
        )
    else:
        row = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='pending')  AS pending,
                   COUNT(*) FILTER (WHERE status='done')     AS done,
                   COUNT(*) FILTER (WHERE status='failed')   AS failed,
                   COUNT(*) FILTER (WHERE status='running')  AS running,
                   COUNT(*) AS total
               FROM global_presence_targets WHERE plan_id=$1""",
            plan_id,
        )
    return (
        dict(row)
        if row
        else {"pending": 0, "done": 0, "failed": 0, "running": 0, "total": 0}
    )


async def get_owner_presence_objects(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    asset_type: str | None = None,
    region: str | None = None,
    plan_id: int | None = None,
    status: str | None = "done",
    limit: int = 50,
    offset: int = 0,
) -> list[asyncpg.Record]:
    """Каталог созданных объектов присутствия владельца (по всем планам-проектам).

    Скоуп по owner_id через JOIN на planы. Фильтры (тип/регион/проект/статус)
    опциональны. Использует только колонки базовой схемы (v35), чтобы не падать
    при лаге v159. status=None → без фильтра по статусу; по умолчанию 'done'.
    """
    conds = ["gpp.owner_id=$1"]
    args: list = [owner_id]
    if status:
        args.append(status)
        conds.append(f"gpt.status=${len(args)}")
    if asset_type:
        args.append(asset_type)
        conds.append(f"gpt.asset_type=${len(args)}")
    if region:
        args.append(region)
        conds.append(f"gpt.region=${len(args)}")
    if plan_id:
        args.append(int(plan_id))
        conds.append(f"gpt.plan_id=${len(args)}")
    args.append(max(1, min(int(limit), 200)))
    lim = f"${len(args)}"
    args.append(max(0, int(offset)))
    off = f"${len(args)}"
    return await pool.fetch(
        f"""SELECT gpt.id, gpt.plan_id, gpt.asset_type, gpt.region, gpt.city,
                   gpt.planned_name, gpt.planned_username, gpt.status, gpt.created_at,
                   gpp.name_pattern
            FROM global_presence_targets gpt
            JOIN global_presence_plans gpp ON gpp.id = gpt.plan_id
            WHERE {' AND '.join(conds)}
            ORDER BY gpt.created_at DESC
            LIMIT {lim} OFFSET {off}""",
        *args,
    )


async def get_owner_presence_regions(
    pool: asyncpg.Pool, owner_id: int, status: str | None = "done"
) -> list[str]:
    """Список непустых регионов среди созданных объектов владельца (для фильтра)."""
    rows = await pool.fetch(
        """SELECT DISTINCT gpt.region
           FROM global_presence_targets gpt
           JOIN global_presence_plans gpp ON gpp.id = gpt.plan_id
           WHERE gpp.owner_id=$1 AND ($2::text IS NULL OR gpt.status=$2)
                 AND gpt.region IS NOT NULL AND gpt.region <> ''
           ORDER BY gpt.region""",
        owner_id,
        status,
    )
    return [r["region"] for r in rows]


async def get_owner_presence_type_counts(
    pool: asyncpg.Pool, owner_id: int, status: str | None = "done"
) -> dict:
    """{asset_type: count} по созданным объектам владельца — для кнопок-фильтров."""
    rows = await pool.fetch(
        """SELECT gpt.asset_type, COUNT(*) AS n
           FROM global_presence_targets gpt
           JOIN global_presence_plans gpp ON gpp.id = gpt.plan_id
           WHERE gpp.owner_id=$1 AND ($2::text IS NULL OR gpt.status=$2)
           GROUP BY gpt.asset_type""",
        owner_id,
        status,
    )
    return {r["asset_type"]: r["n"] for r in rows}


async def reset_failed_targets(
    pool: asyncpg.Pool, plan_id: int, owner_id: int | None = None
) -> int:
    """Reset failed+retryable targets to pending for retry. Returns count reset."""
    if owner_id is not None:
        result = await pool.execute(
            "UPDATE global_presence_targets SET status='pending', error_message=NULL "
            "WHERE plan_id=$1 AND status='failed' AND retryable=TRUE "
            "AND EXISTS (SELECT 1 FROM global_presence_plans WHERE id=$1 AND owner_id=$2)",
            plan_id,
            owner_id,
        )
    else:
        result = await pool.execute(
            "UPDATE global_presence_targets SET status='pending', error_message=NULL "
            "WHERE plan_id=$1 AND status='failed' AND retryable=TRUE",
            plan_id,
        )
    return int(result.split()[-1]) if result else 0


async def link_plan_to_operation(pool: asyncpg.Pool, plan_id: int, op_id: int) -> None:
    await pool.execute(
        "UPDATE global_presence_plans SET op_id=$1, status='queued', updated_at=now() WHERE id=$2",
        op_id,
        plan_id,
    )


async def cancel_global_presence_plan(
    pool: asyncpg.Pool, plan_id: int, owner_id: int
) -> bool:
    """Cancel a plan and its linked operation. Returns True if the plan was found and owned."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            plan = await conn.fetchrow(
                "SELECT op_id FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
                plan_id,
                owner_id,
            )
            if not plan:
                return False
            await conn.execute(
                "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1",
                plan_id,
            )
            await conn.execute(
                "UPDATE global_presence_targets SET status='cancelled' WHERE plan_id=$1 AND status='pending'",
                plan_id,
            )
            if plan["op_id"]:
                await conn.execute(
                    # 'partial' — тоже завершённое состояние (services/op_status):
                    # отмена плана не должна задним числом переписывать исход
                    # операции, которая УЖЕ отработала часть целей.
                    "UPDATE operation_queue SET status='cancelled', finished_at=now() "
                    "WHERE id=$1 AND status NOT IN ('done','partial','failed','cancelled')",
                    plan["op_id"],
                )
    return True


async def sync_plan_status_from_op(pool: asyncpg.Pool, plan_id: int) -> str | None:
    """If operation is done/failed but plan still running, fix plan status. Returns new status or None."""
    row = await pool.fetchrow(
        """SELECT p.status AS plan_status, p.op_id, o.status AS op_status
           FROM global_presence_plans p
           LEFT JOIN operation_queue o ON o.id=p.op_id
           WHERE p.id=$1""",
        plan_id,
    )
    if not row:
        return None
    if row["plan_status"] not in ("running", "queued"):
        return None
    op_status = row["op_status"]
    if op_status in ("done", "failed", "cancelled"):
        new_status = op_status
        await pool.execute(
            "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
            new_status,
            plan_id,
        )
        return new_status
    return None


# ── Operation Reports and Statistics ──────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────
# Gift Transfer Database Functions
# ─────────────────────────────────────────────────────────────────────────


async def create_gift_transfer_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    recipient_username: str,
    recipient_user_id: int,
    recipient_name: str,
    payment_source: str,
    payment_method_id: int | None = None,
) -> int:
    """Create a gift transfer plan. Returns plan_id."""
    return await pool.fetchval(
        """INSERT INTO gift_transfer_plans
               (owner_id, recipient_username, recipient_user_id, recipient_name,
                payment_source, payment_method_id, status)
           VALUES ($1, $2, $3, $4, $5, $6, 'pending')
           RETURNING id""",
        owner_id,
        recipient_username,
        recipient_user_id,
        recipient_name,
        payment_source,
        payment_method_id,
    )


async def get_gift_transfer_plan(
    pool: asyncpg.Pool, plan_id: int, owner_id: int
) -> dict | None:
    """Get a gift transfer plan."""
    row = await pool.fetchrow(
        "SELECT * FROM gift_transfer_plans WHERE id=$1 AND owner_id=$2",
        plan_id,
        owner_id,
    )
    return dict(row) if row else None


async def get_gift_transfer_items(pool: asyncpg.Pool, plan_id: int) -> list[dict]:
    """Get all items in a gift transfer plan."""
    rows = await pool.fetch(
        "SELECT * FROM gift_transfer_items WHERE plan_id=$1 ORDER BY id", plan_id
    )
    return [dict(r) for r in rows]


async def get_gift_transfer_stats(pool: asyncpg.Pool, plan_id: int) -> dict:
    """Get transfer stats for a plan."""
    row = await pool.fetchrow(
        """
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status='transferred') as transferred,
            COUNT(*) FILTER (WHERE status='failed') as failed,
            COUNT(*) FILTER (WHERE status='skipped') as skipped,
            COUNT(*) FILTER (WHERE status='pending_confirmation') as pending,
            COUNT(*) FILTER (WHERE status IN ('pending','queued')) as remaining,
            SUM(stars_cost) FILTER (WHERE status='transferred') as actual_cost
        FROM gift_transfer_items
        WHERE plan_id=$1
    """,
        plan_id,
    )
    return dict(row) if row else {}


_ALLOWED_GIFT_PLAN_FIELDS: frozenset[str] = frozenset({
    "name", "recipient_username", "recipient_user_id", "recipient_name",
    "payment_source", "payment_method_id", "status",
    "total_gifts", "selected_gifts", "estimated_cost", "actual_cost",
    "error_message", "completed_at",
})


async def update_gift_transfer_plan(pool: asyncpg.Pool, plan_id: int, **kwargs) -> None:
    """Update gift transfer plan fields."""
    if not kwargs:
        return
    safe = {k: v for k, v in kwargs.items() if k in _ALLOWED_GIFT_PLAN_FIELDS}
    if not safe:
        return
    set_clause = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(safe.keys()))
    await pool.execute(
        f"UPDATE gift_transfer_plans SET {set_clause}, updated_at=now() WHERE id=$1",
        plan_id,
        *safe.values(),
    )


async def get_gift_recipients(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Get saved recipients for a user."""
    rows = await pool.fetch(
        "SELECT * FROM gift_recipients WHERE owner_id=$1 ORDER BY is_main_admin DESC, name",
        owner_id,
    )
    return [dict(r) for r in rows]


async def save_gift_recipient(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    username: str,
    user_id: int | None = None,
    is_main_admin: bool = False,
) -> int:
    """Save a recipient. Returns recipient_id."""
    return await pool.fetchval(
        """INSERT INTO gift_recipients
               (owner_id, name, username, user_id, is_main_admin)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (owner_id, name) DO UPDATE SET
               username = EXCLUDED.username,
               user_id = EXCLUDED.user_id,
               updated_at = now()
           RETURNING id""",
        owner_id,
        name,
        username,
        user_id,
        is_main_admin,
    )


async def delete_gift_recipient(
    pool: asyncpg.Pool, owner_id: int, recipient_id: int
) -> bool:
    """Delete a saved recipient. Returns True if deleted."""
    result = await pool.execute(
        "DELETE FROM gift_recipients WHERE id=$1 AND owner_id=$2",
        recipient_id,
        owner_id,
    )
    return "DELETE 1" in result


async def get_gift_transfer_reports(
    pool: asyncpg.Pool, owner_id: int, limit: int = 10
) -> list[dict]:
    """Get recent gift transfer reports."""
    rows = await pool.fetch(
        """SELECT * FROM gift_transfer_reports
           WHERE owner_id=$1
           ORDER BY created_at DESC
           LIMIT $2""",
        owner_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_gift_transfer_report(
    pool: asyncpg.Pool, report_id: int, owner_id: int
) -> dict | None:
    """Get a specific report."""
    row = await pool.fetchrow(
        "SELECT * FROM gift_transfer_reports WHERE id=$1 AND owner_id=$2",
        report_id,
        owner_id,
    )
    return dict(row) if row else None


async def get_operation_stats(pool: asyncpg.Pool, owner_id: int, op_id: int) -> dict:
    """Получить полную статистику выполненной операции.
    
    НЕ ИСПОЛЬЗУЕТСЯ: вызовов нет ни в боте, ни в мини-аппе. Запрос был написан
    по несуществующей колонке `updated_at` и исправлен на настоящую схему —
    иначе первый же вызов после подключения упал бы.
    """
    op = await pool.fetchrow(
        # updated_at в operation_queue нет — запрос падал, и статистика операции
        # не открывалась вовсе. Ближайшее по смыслу — момент завершения.
        "SELECT id, op_type, status, total_items, done_items, params, created_at, "
        "finished_at, started_at "
        "FROM operation_queue WHERE id=$1 AND owner_id=$2",
        op_id,
        owner_id,
    )
    if not op:
        return {}

    logs = await pool.fetch(
        "SELECT step_num, target, status, message FROM operation_log "
        "WHERE op_id=$1 ORDER BY step_num",
        op_id,
    )

    errors = [l for l in logs if l["status"] != "ok"]
    success_count = len([l for l in logs if l["status"] == "ok"])

    return {
        "id": op["id"],
        "type": op["op_type"],
        "status": op["status"],
        "total": op["total_items"],
        "done": op["done_items"],
        "success": success_count,
        "errors": len(errors),
        "created_at": op["created_at"],
        # Имя поля в ответе оставляем прежним, значение берём из finished_at.
        # Длительность считаем от СТАРТА, а не от постановки в очередь: операция
        # может пролежать в очереди часы, и «выполнялась 4 часа» — это неправда.
        "updated_at": op["finished_at"],
        "duration_seconds": (
            (op["finished_at"] - (op["started_at"] or op["created_at"])).total_seconds()
            if op["finished_at"] else 0
        ),
        "error_details": errors[:10],  # top 10 errors
    }


async def get_user_operation_history(
    pool: asyncpg.Pool, owner_id: int, limit: int = 20
) -> list[dict]:
    """Получить историю операций пользователя."""
    rows = await pool.fetch(
        "SELECT id, op_type, status, total_items, done_items, created_at "
        "FROM operation_queue WHERE owner_id=$1 "
        "ORDER BY created_at DESC LIMIT $2",
        owner_id,
        limit,
    )
    return [dict(r) for r in rows]


async def count_operation_errors(pool: asyncpg.Pool, op_id: int) -> int:
    """Сколько ошибок в логе операции."""
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_log WHERE op_id=$1 AND status != 'ok'",
        op_id,
    )
    return count or 0


# ── Platform Users Management (v39) ──────────────────────────────────────────


async def register_or_update_user(
    pool: asyncpg.Pool, user_id: int, username: str = None, first_name: str = None
) -> None:
    """Регистрировать нового или обновить существующего пользователя."""
    try:
        await pool.execute(
            """INSERT INTO platform_users (user_id, username, first_name, last_seen, trial_started_at)
               VALUES ($1, $2, $3, now(), now())
               ON CONFLICT (user_id) DO UPDATE
               SET username = COALESCE($2, platform_users.username),
                   first_name = COALESCE($3, platform_users.first_name),
                   last_seen = now()""",
            user_id,
            username,
            first_name,
        )
    except asyncpg.UndefinedColumnError:
        # Совместимость: старая схема v14 использует last_active
        await pool.execute(
            """INSERT INTO platform_users (user_id, username, first_name, last_active)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (user_id) DO UPDATE
               SET username = COALESCE($2, platform_users.username),
                   first_name = COALESCE($3, platform_users.first_name),
                   last_active = now()""",
            user_id,
            username,
            first_name,
        )


async def get_all_platform_users(
    pool: asyncpg.Pool,
    limit: int = 50,
    offset: int = 0,
    plan: str = None,
    is_banned: bool = None,
) -> list[dict]:
    """Получить список всех пользователей с фильтрацией."""
    query = (
        "SELECT user_id, username, first_name, "
        "COALESCE(current_plan, 'free') as current_plan, "
        "plan_expires_at, "
        "COALESCE(is_banned, false) as is_banned, "
        "COALESCE(registered_at, first_seen, created_at) as registered_at "
        "FROM platform_users WHERE 1=1"
    )
    params = []

    if plan:
        query += " AND COALESCE(current_plan,'free')=$" + str(len(params) + 1)
        params.append(plan)

    if is_banned is not None:
        query += " AND COALESCE(is_banned,false)=$" + str(len(params) + 1)
        params.append(is_banned)

    query += (
        " ORDER BY COALESCE(registered_at, first_seen) DESC NULLS LAST LIMIT $"
        + str(len(params) + 1)
        + " OFFSET $"
        + str(len(params) + 2)
    )
    params.extend([limit, offset])

    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


async def grant_plan_to_user(
    pool: asyncpg.Pool,
    user_id: int,
    admin_id: int,
    plan: str,
    months: int,
    *,
    record_payment: bool = True,
) -> None:
    """Выдать план пользователю (админ-действие). Пишет в обе таблицы.

    record_payment=True: фиксирует подтверждённую оплату в payments
    (ручная выдача обычно делается после получения денег), чтобы выручка
    и счётчик «Подтверждённых оплат» в админке отражали реальность.
    Передайте record_payment=False для бесплатной/промо-выдачи.
    """
    from datetime import datetime, timedelta, timezone

    expires = datetime.now(timezone.utc) + timedelta(days=30 * months)

    # subscriptions — источник правды (тут get_plan() проверяет доступ). Пишем
    # ЕЁ первой, а platform_users синхронизируем ровно от возвращённого
    # expires_at, а не вторым независимым CASE — иначе две даты могут разойтись.
    # Обе таблицы прав — одной транзакцией. Комментарий выше требует, чтобы
    # даты не разошлись, но два независимых запроса этого не гарантируют:
    # обрыв между ними (передеплой, разрыв соединения) оставлял подписку
    # активной при 'free' в platform_users или наоборот. Платёж пишется тут же
    # — выданный доступ без записи об оплате искажает выручку.
    async with pool.acquire() as conn:
        async with conn.transaction():
            if plan == "free":
                await conn.execute(
                    "UPDATE subscriptions SET is_active=false WHERE user_id=$1", user_id
                )
                await conn.execute(
                    "UPDATE platform_users SET current_plan='free', plan_expires_at=NULL WHERE user_id=$1",
                    user_id,
                )
            else:
                sub_row = await conn.fetchrow(
                    """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
                       VALUES($1, $2, now() + ($3 || ' months')::INTERVAL, true)
                       ON CONFLICT(user_id) DO UPDATE
                       SET plan      = EXCLUDED.plan,
                           is_active = true,
                           expires_at = CASE
                               WHEN subscriptions.expires_at > now()
                                   THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                               ELSE now() + ($3 || ' months')::INTERVAL
                           END
                       RETURNING plan, expires_at""",
                    user_id,
                    plan,
                    str(months),
                )
                if sub_row:
                    expires = sub_row["expires_at"]
                    await conn.execute(
                        "UPDATE platform_users SET current_plan=$2, plan_expires_at=$3 WHERE user_id=$1",
                        user_id,
                        sub_row["plan"],
                        sub_row["expires_at"],
                    )

                # Зафиксировать подтверждённую оплату, чтобы выручка/счётчики
                # были точными. Вложенная транзакция — это SAVEPOINT: сбой
                # записи платежа откатывает только её, а выданный доступ
                # остаётся, как и было задумано здесь изначально.
                if record_payment:
                    base = PLAN_PRICES_USD.get(plan, 0)
                    disc = PERIOD_DISCOUNTS.get(months, 0)
                    amount_usd = round(base * months * (1 - disc / 100), 2)
                    reference = f"ADMIN-{admin_id}-{user_id}-{int(time.time())}"
                    try:
                        async with conn.transaction():
                            await conn.execute(
                                """INSERT INTO payments
                                       (user_id, plan, period_months, currency, amount_crypto,
                                        amount_usd, wallet_address, reference, status, confirmed_at)
                                   VALUES ($1,$2,$3,'USDT_TRC20',$4,$4,'manual',$5,'confirmed', now())
                                   ON CONFLICT (reference) DO NOTHING""",
                                user_id,
                                plan,
                                months,
                                amount_usd,
                                reference,
                            )
                    except Exception:
                        log_exc_swallow(
                            log,
                            "grant_plan_to_user: failed to record payment",
                            admin_id=admin_id,
                            user_id=user_id,
                        )

    # Логировать действие
    try:
        await pool.execute(
            """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
               VALUES ($1, 'grant_plan', $2, $3)""",
            admin_id,
            user_id,
            json.dumps(
                {"plan": plan, "months": months, "expires_at": expires.isoformat()},
                ensure_ascii=False,
            ),
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой записи admin_audit_log", admin_id=admin_id, user_id=user_id
        )

    # Обе таблицы записаны выше — сбрасываем кеш плана, иначе get_plan отдаёт
    # старое значение до 60с (в т.ч. в мини-аппе — тот же процесс, тот же кеш).
    _invalidate_plan_cache(user_id)


async def grant_subscription(
    pool: asyncpg.Pool,
    user_id: int,
    plan: str,
    days: int,
    granted_by: int,
    *,
    record_payment: bool = True,
) -> None:
    """Admin UI alias: grant plan for N days (converts to months, min 1)."""
    months = max(1, days // 30)
    await grant_plan_to_user(
        pool, user_id, granted_by, plan, months, record_payment=record_payment
    )


async def revoke_plan_from_user(
    pool: asyncpg.Pool, user_id: int, admin_id: int
) -> None:
    """Забрать подписку у пользователя (вернуть на free)."""
    # Обе таблицы — одной транзакцией. Права проверяются по subscriptions, а
    # админка показывает platform_users: обрыв между двумя UPDATE оставлял
    # пользователя с платным доступом при надписи «free» в админке, то есть
    # отзыв выглядел выполненным, а на деле не действовал.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE platform_users SET current_plan='free', plan_expires_at=NULL WHERE user_id=$1",
                user_id,
            )
            # Отключить в subscriptions (именно здесь get_plan() проверяет)
            await conn.execute(
                "UPDATE subscriptions SET is_active=false WHERE user_id=$1", user_id
            )

    # Логировать действие. Отдельно от транзакции и со снятием исключения:
    # отзыв доступа уже состоялся, и ронять его из-за журнала нельзя — но и
    # промолчать о несостоявшейся записи тоже.
    try:
        await pool.execute(
            """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
               VALUES ($1, 'revoke_plan', $2, '{}')""",
            admin_id,
            user_id,
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой записи admin_audit_log", admin_id=admin_id, user_id=user_id
        )
    # Сброс кеша — иначе отозванный юзер до 60с считается платным.
    _invalidate_plan_cache(user_id)


async def revoke_strike_access(pool: asyncpg.Pool, user_id: int, admin_id: int) -> None:
    """Забрать Strike доступ у пользователя."""
    await pool.execute("DELETE FROM strike_access WHERE user_id=$1", user_id)

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'revoke_strike', $2, '{}')""",
        admin_id,
        user_id,
    )


async def ban_user(
    pool: asyncpg.Pool, user_id: int, admin_id: int, reason: str = None
) -> None:
    """Забанить пользователя."""
    await pool.execute(
        """UPDATE platform_users
           SET is_banned=true, ban_reason=$1, banned_at=now()
           WHERE user_id=$2""",
        reason,
        user_id,
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'ban_user', $2, $3)""",
        admin_id,
        user_id,
        json.dumps({"reason": reason or ""}),
    )


async def unban_user(pool: asyncpg.Pool, user_id: int, admin_id: int) -> None:
    """Разбанить пользователя."""
    await pool.execute(
        """UPDATE platform_users
           SET is_banned=false, ban_reason=NULL, banned_at=NULL
           WHERE user_id=$1""",
        user_id,
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'unban_user', $2, '{}')""",
        admin_id,
        user_id,
    )


async def get_user_info(pool: asyncpg.Pool, user_id: int) -> dict:
    """Получить полную информацию о пользователе."""
    row = await pool.fetchrow(
        "SELECT * FROM platform_users WHERE user_id=$1",
        user_id,
    )
    return dict(row) if row else None


async def log_security_violation(
    pool: asyncpg.Pool,
    user_id: int,
    attempt_type: str,
    details: dict = None,
    ip_address: str = None,
) -> None:
    """Логировать попытку несанкционированного доступа или подозрительную активность."""
    await pool.execute(
        """INSERT INTO security_violations (user_id, attempt_type, details, ip_address)
           VALUES ($1, $2, $3, $4)""",
        user_id,
        attempt_type,
        str(details or {}),
        ip_address,
    )


async def count_platform_users(
    pool: asyncpg.Pool, plan: str = None, is_banned: bool = None
) -> int:
    """Подсчитать количество пользователей с фильтрацией."""
    query = "SELECT COUNT(*) FROM platform_users WHERE 1=1"
    params = []

    if plan:
        query += " AND current_plan=$" + str(len(params) + 1)
        params.append(plan)

    if is_banned is not None:
        query += " AND is_banned=$" + str(len(params) + 1)
        params.append(is_banned)

    count = await pool.fetchval(query, *params)
    return count or 0


async def get_admin_audit_log(
    pool: asyncpg.Pool, admin_id: int = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Получить лог админ-действий."""
    if admin_id:
        rows = await pool.fetch(
            """SELECT id, admin_id, action, target_user_id, details, created_at
               FROM admin_audit_log
               WHERE admin_id=$1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            admin_id,
            limit,
            offset,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, admin_id, action, target_user_id, details, created_at
               FROM admin_audit_log
               ORDER BY created_at DESC LIMIT $1 OFFSET $2""",
            limit,
            offset,
        )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# BOT ADMIN SESSIONS
# ══════════════════════════════════════════════════════════════════


async def upsert_bot_admin_session(
    pool: asyncpg.Pool, bot_id: int, owner_id: int, token: str
) -> None:
    await pool.execute(
        "INSERT INTO bot_admin_sessions(bot_id,owner_id,token) VALUES($1,$2,$3) "
        "ON CONFLICT(bot_id) DO UPDATE SET token=$3, owner_id=$2",
        bot_id,
        owner_id,
        token,
    )


async def get_bot_admin_session_by_token(pool: asyncpg.Pool, token: str):
    return await pool.fetchrow(
        "SELECT bot_id, owner_id FROM bot_admin_sessions WHERE token=$1", token
    )


async def get_bot_admin_token(pool: asyncpg.Pool, bot_id: int) -> str | None:
    row = await pool.fetchrow(
        "SELECT token FROM bot_admin_sessions WHERE bot_id=$1", bot_id
    )
    return row["token"] if row else None


# ══════════════════════════════════════════════════════════════════
# PRESENCE PACKS
# ══════════════════════════════════════════════════════════════════


async def create_presence_pack(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    description: str | None = None,
    target_url: str | None = None,
    target_label: str | None = None,
    bot_id: int | None = None,
    bot_username: str | None = None,
) -> int:
    return await pool.fetchval(
        "INSERT INTO presence_packs(owner_id,name,description,target_url,target_label,bot_id,bot_username) "
        "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
        owner_id,
        name,
        description,
        target_url,
        target_label,
        bot_id,
        bot_username,
    )


async def get_presence_pack(pool: asyncpg.Pool, pack_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, owner_id
    )


async def get_presence_packs(pool: asyncpg.Pool, owner_id: int, limit: int = 15):
    return await pool.fetch(
        "SELECT * FROM presence_packs WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
        owner_id,
        limit,
    )


async def update_presence_pack_channels(
    pool: asyncpg.Pool,
    pack_id: int,
    owner_id: int,
    channel_ids: list[int],
    group_ids: list[int],
) -> None:

    await pool.execute(
        "UPDATE presence_packs SET channel_ids=$3, group_ids=$4 WHERE id=$1 AND owner_id=$2",
        pack_id,
        owner_id,
        json.dumps(channel_ids, ensure_ascii=False),
        json.dumps(group_ids, ensure_ascii=False),
    )


async def mark_presence_pack_seeded(
    pool: asyncpg.Pool, pack_id: int, owner_id: int
) -> None:
    await pool.execute(
        "UPDATE presence_packs SET seed_posted=TRUE WHERE id=$1 AND owner_id=$2",
        pack_id,
        owner_id,
    )


async def mark_presence_pack_promoted(
    pool: asyncpg.Pool, pack_id: int, owner_id: int
) -> None:
    await pool.execute(
        "UPDATE presence_packs SET bot_promoted=TRUE WHERE id=$1 AND owner_id=$2",
        pack_id,
        owner_id,
    )


async def delete_presence_pack(pool: asyncpg.Pool, pack_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, owner_id
    )


async def enqueue_op_with_approval(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    params: dict,
    total_items: int,
    threshold: int = 20,
) -> int:
    """Enqueue operation. If total_items > threshold, set requires_approval=TRUE."""
    import json as _json

    needs_approval = total_items > threshold
    status = "waiting_approval" if needs_approval else "pending"
    row = await pool.fetchrow(
        """INSERT INTO operation_queue
           (owner_id, op_type, params, total_items, status, requires_approval, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,now()) RETURNING id""",
        owner_id,
        op_type,
        _json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else params,
        total_items,
        status,
        needs_approval,
    )
    return row["id"]


async def get_pending_approvals(pool: asyncpg.Pool, owner_id: int) -> list:
    return await pool.fetch(
        """SELECT id, op_type, total_items, created_at
           FROM operation_queue
           WHERE owner_id=$1 AND status='waiting_approval'
           ORDER BY created_at DESC LIMIT 10""",
        owner_id,
    )


# ── Workspaces ────────────────────────────────────────────────────────────────


async def create_workspace(
    pool: asyncpg.Pool, owner_id: int, name: str, description: str = ""
) -> int:
    # Одна транзакция: workspace без строки владельца в workspace_members —
    # это workspace, в который никто не может войти, в том числе его создатель,
    # потому что доступ считается по членству. Обрыв между двумя вставками
    # (передеплой, разрыв соединения) оставлял ровно такой объект.
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO workspaces (owner_id, name, description) VALUES ($1,$2,$3) RETURNING id",
                owner_id,
                name[:64],
                description[:256],
            )
            ws_id = row["id"]
            await conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role, invited_by) VALUES ($1,$2,'owner',$2)",
                ws_id,
                owner_id,
            )
    return ws_id


async def get_user_workspaces(pool: asyncpg.Pool, user_id: int) -> list:
    return await pool.fetch(
        """SELECT w.id, w.name, w.description, wm.role, w.owner_id,
                  (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=w.id) AS member_count
           FROM workspaces w
           JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=$1
           WHERE w.is_active=TRUE
           ORDER BY w.created_at""",
        user_id,
    )


# Роли workspace по убыванию прав. Приглашать и управлять составом может
# владелец и админ; участник и наблюдатель — только смотреть.
WORKSPACE_ROLES = ("owner", "admin", "member", "viewer")
WORKSPACE_INVITE_ROLES = ("owner", "admin")
# Сколько живёт код-приглашение в workspace. Вход в workspace даёт доступ к
# ботам и каналам его владельца, поэтому код — секрет с ограниченным сроком,
# а не постоянный ключ.
WORKSPACE_INVITE_TTL_DAYS = 7


async def get_workspace_role(
    pool: asyncpg.Pool, ws_id: int, user_id: int
) -> str | None:
    """Роль пользователя в workspace, либо None, если он не участник.

    Единственный источник правды о доступе к workspace. Номер workspace
    приходит из callback_data кнопки, то есть фактически от клиента: данные
    кнопки отправляет клиент, а не Telegram, и у всех пользователей этого
    продукта есть MTProto-клиент. Поэтому ни один обработчик не имеет права
    считать, что раз номер пришёл — доступ есть.
    """
    return await pool.fetchval(
        """SELECT wm.role FROM workspace_members wm
           JOIN workspaces w ON w.id = wm.workspace_id AND w.is_active = TRUE
           WHERE wm.workspace_id=$1 AND wm.user_id=$2""",
        ws_id,
        user_id,
    )


async def get_workspace(
    pool: asyncpg.Pool, ws_id: int, viewer_id: int | None = None
) -> dict | None:
    """Workspace по номеру. С viewer_id — только если тот в нём состоит.

    viewer_id необязателен ради фоновых вызовов, где пользователя нет, но
    любой путь, куда номер пришёл от человека, обязан его передавать.
    """
    if viewer_id is not None:
        row = await pool.fetchrow(
            """SELECT w.* FROM workspaces w
               JOIN workspace_members wm
                 ON wm.workspace_id = w.id AND wm.user_id = $2
               WHERE w.id=$1 AND w.is_active=TRUE""",
            ws_id,
            viewer_id,
        )
    else:
        row = await pool.fetchrow(
            "SELECT * FROM workspaces WHERE id=$1 AND is_active=TRUE", ws_id
        )
    return dict(row) if row else None


async def get_workspace_members(
    pool: asyncpg.Pool, ws_id: int, viewer_id: int | None = None
) -> list:
    """Состав workspace. С viewer_id — пустой список для постороннего.

    Список участников — это чужие user_id и имена, то есть персональные
    данные; отдавать их по одному лишь номеру workspace нельзя.
    """
    if viewer_id is not None and not await get_workspace_role(pool, ws_id, viewer_id):
        return []
    return await pool.fetch(
        """SELECT wm.user_id, wm.role, wm.joined_at, pu.username, pu.first_name
           FROM workspace_members wm
           LEFT JOIN platform_users pu ON pu.user_id=wm.user_id
           WHERE wm.workspace_id=$1 ORDER BY wm.joined_at""",
        ws_id,
    )


async def create_workspace_invite(
    pool: asyncpg.Pool, ws_id: int, created_by: int
) -> str | None:
    """Код-приглашение в workspace. None, если у создателя нет на это прав.

    Раньше код выписывался по одному номеру workspace: посторонний выпускал
    себе приглашение в чужой workspace и входил в него участником. Право
    приглашать проверяется здесь, а не только в обработчике, потому что
    обработчиков со временем станет больше одного.
    """
    role = await get_workspace_role(pool, ws_id, created_by)
    if role not in WORKSPACE_INVITE_ROLES:
        log.warning(
            "workspace invite отклонён: user=%s ws=%s роль=%s",
            created_by, ws_id, role or "не участник",
        )
        return None

    import secrets as _sec

    code = _sec.token_urlsafe(12)
    await pool.execute(
        "INSERT INTO workspace_invites (workspace_id, invite_code, created_by, uses_left, expires_at) "
        f"VALUES ($1,$2,$3,5, now() + INTERVAL '{WORKSPACE_INVITE_TTL_DAYS} days')",
        ws_id,
        code,
        created_by,
    )
    return code


async def use_workspace_invite(
    pool: asyncpg.Pool, code: str, user_id: int
) -> int | None:
    """Use invite code. Returns workspace_id on success, None if invalid/expired."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            invite = await conn.fetchrow(
                # Срок жизни обязателен. Колонка expires_at была, но её никто не
                # заполнял и никто не проверял: код на пять входов работал
                # вечно. А вход в workspace — это доступ к ботам владельца
                # (get_bot отдаёт их с РАСШИФРОВАННЫМ токеном) и к его каналам,
                # так что утёкший полгода назад код оставался рабочим ключом.
                # У старых строк expires_at пуст — считаем их живущими те же
                # дни от создания, а не вечно.
                "SELECT * FROM workspace_invites "
                " WHERE invite_code=$1 AND uses_left>0 "
                f"   AND now() < COALESCE(expires_at, created_at + INTERVAL '{WORKSPACE_INVITE_TTL_DAYS} days') "
                " FOR UPDATE",
                code,
            )
            if not invite:
                return None
            ws_id = invite["workspace_id"]
            existing = await conn.fetchval(
                "SELECT 1 FROM workspace_members WHERE workspace_id=$1 AND user_id=$2",
                ws_id,
                user_id,
            )
            if not existing:
                await conn.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role, invited_by) VALUES ($1,$2,'member',$3)",
                    ws_id,
                    user_id,
                    invite["created_by"],
                )
                await conn.execute(
                    "UPDATE workspace_invites SET uses_left=uses_left-1 WHERE invite_code=$1",
                    code,
                )
    return ws_id


async def delete_workspace_member(pool: asyncpg.Pool, ws_id: int, user_id: int) -> None:
    await pool.execute(
        "DELETE FROM workspace_members WHERE workspace_id=$1 AND user_id=$2",
        ws_id,
        user_id,
    )


async def get_platform_setting(pool: asyncpg.Pool, key: str, default: str = "") -> str:
    row = await pool.fetchrow("SELECT value FROM platform_settings WHERE key=$1", key)
    return row["value"] if row else default


async def set_platform_setting(pool: asyncpg.Pool, key: str, value: str) -> None:
    await pool.execute(
        """INSERT INTO platform_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
        key,
        value,
    )


# ── Account Infrastructure (v60) ─────────────────────────────────────────────


async def update_account_tags(
    pool: asyncpg.Pool, acc_id: int, owner_id: int, tags: list[str]
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET tags=$1 WHERE id=$2 AND owner_id=$3",
        tags,
        acc_id,
        owner_id,
    )


async def update_account_pool(
    pool: asyncpg.Pool, acc_id: int, owner_id: int, pool_name: str | None
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET pool=$1 WHERE id=$2 AND owner_id=$3",
        pool_name,
        acc_id,
        owner_id,
    )


async def update_account_labels(
    pool: asyncpg.Pool, acc_id: int, owner_id: int, labels: list[str]
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET labels=$1 WHERE id=$2 AND owner_id=$3",
        labels,
        acc_id,
        owner_id,
    )


async def update_account_warnings(
    pool: asyncpg.Pool, acc_id: int, owner_id: int, warnings: list[str]
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET warnings=$1 WHERE id=$2 AND owner_id=$3",
        warnings,
        acc_id,
        owner_id,
    )


async def update_account_project(
    pool: asyncpg.Pool, acc_id: int, owner_id: int, project: str | None
) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET project=$1 WHERE id=$2 AND owner_id=$3",
        project,
        acc_id,
        owner_id,
    )


async def get_accounts_by_pool(
    pool: asyncpg.Pool, owner_id: int, pool_name: str
) -> list:
    return await pool.fetch(
        """SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                  a.trust_score, a.cooldown_until, a.tags, a.pool, a.labels, a.warnings, a.project,
                  a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.pool=$2 AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST""",
        owner_id,
        pool_name,
    )


async def get_accounts_by_tags(
    pool: asyncpg.Pool, owner_id: int, tags: list[str]
) -> list:
    """Return accounts that have ALL of the specified tags."""
    return await pool.fetch(
        """SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                  a.trust_score, a.cooldown_until, a.tags, a.pool, a.labels, a.warnings, a.project,
                  a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.tags @> $2::text[] AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST""",
        owner_id,
        tags,
    )


async def get_distinct_pools(pool: asyncpg.Pool, owner_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT pool FROM tg_accounts WHERE owner_id=$1 AND pool IS NOT NULL ORDER BY pool",
        owner_id,
    )
    return [r["pool"] for r in rows]


async def get_distinct_tags(pool: asyncpg.Pool, owner_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT unnest(tags) AS tag FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE ORDER BY tag",
        owner_id,
    )
    return [r["tag"] for r in rows]


async def get_account_assets(pool: asyncpg.Pool, acc_id: int, owner_id: int) -> dict:
    """Return all assets associated with an account (for disaster recovery)."""
    channels = await pool.fetch(
        "SELECT channel_id, title, username FROM managed_channels WHERE acc_id=$1 AND owner_id=$2 ORDER BY title",
        acc_id,
        owner_id,
    )
    active_ops = await pool.fetch(
        """SELECT id, op_type, status, total_items, done_items, created_at
           FROM operation_queue
           WHERE owner_id=$1 AND status IN ('pending','running')
             AND params::text LIKE $2
           ORDER BY created_at DESC LIMIT 20""",
        owner_id,
        f'%"acc_id": {acc_id}%',
    )
    return {"channels": list(channels), "ops": list(active_ops)}


# ── Proxy Intelligence (v60) ──────────────────────────────────────────────────


async def log_proxy_quality(
    pool: asyncpg.Pool,
    proxy_id: int,
    latency_ms: int | None,
    success: bool,
    error_msg: str | None = None,
) -> None:
    await pool.execute(
        "INSERT INTO proxy_quality_log (proxy_id, latency_ms, success, error_msg) VALUES ($1,$2,$3,$4)",
        proxy_id,
        latency_ms,
        success,
        error_msg,
    )


async def get_proxy_quality_stats(pool: asyncpg.Pool, proxy_id: int) -> dict:
    row = await pool.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE success) AS successes,
               COUNT(*) FILTER (WHERE NOT success) AS failures,
               COUNT(*) AS total,
               AVG(latency_ms) FILTER (WHERE success AND latency_ms IS NOT NULL) AS avg_latency,
               MAX(checked_at) AS last_checked
           FROM proxy_quality_log
           WHERE proxy_id=$1 AND checked_at > NOW() - INTERVAL '7 days'""",
        proxy_id,
    )
    if not row or row["total"] == 0:
        return {
            "success_rate": None,
            "avg_latency": None,
            "total": 0,
            "last_checked": None,
        }
    total = row["total"] or 1
    return {
        "success_rate": round((row["successes"] or 0) / total * 100, 1),
        "avg_latency": round(row["avg_latency"]) if row["avg_latency"] else None,
        "total": total,
        "successes": row["successes"] or 0,
        "failures": row["failures"] or 0,
        "last_checked": row["last_checked"],
    }


async def get_all_proxy_quality_stats(pool: asyncpg.Pool, owner_id: int) -> list:
    """Return quality stats for all proxies owned by user."""
    rows = await pool.fetch(
        """SELECT p.id, p.label, p.proxy_url,
               COUNT(q.id) FILTER (WHERE q.success) AS successes,
               COUNT(q.id) FILTER (WHERE NOT q.success) AS failures,
               COUNT(q.id) AS total,
               AVG(q.latency_ms) FILTER (WHERE q.success AND q.latency_ms IS NOT NULL) AS avg_latency
           FROM user_proxies p
           LEFT JOIN proxy_quality_log q
               ON q.proxy_id=p.id AND q.checked_at > NOW() - INTERVAL '7 days'
           WHERE p.owner_id=$1
           GROUP BY p.id, p.label, p.proxy_url
           ORDER BY p.label""",
        owner_id,
    )
    # proxy_url хранится зашифрованным (token_vault) — расшифровываем для потребителя,
    # как во всех остальных листьях (passthrough legacy plaintext).
    from services.token_vault import decrypt_token

    result = []
    for r in rows:
        total = r["total"] or 0
        result.append(
            {
                "id": r["id"],
                "label": r["label"],
                "proxy_url": decrypt_token(r["proxy_url"]) if r["proxy_url"] else r["proxy_url"],
                "success_rate": round((r["successes"] or 0) / total * 100, 1)
                if total > 0
                else None,
                "avg_latency": round(r["avg_latency"]) if r["avg_latency"] else None,
                "total": total,
            }
        )
    return result


# ── Infrastructure Pressure Score cache ──────────────────────────────────────


async def save_pressure_cache(
    pool: asyncpg.Pool, owner_id: int, score: int, breakdown: dict
) -> None:

    await pool.execute(
        """INSERT INTO infra_pressure_cache (owner_id, pressure_score, breakdown, computed_at)
           VALUES ($1, $2, $3, NOW())
           ON CONFLICT (owner_id) DO UPDATE
           SET pressure_score=$2, breakdown=$3, computed_at=NOW()""",
        owner_id,
        score,
        json.dumps(breakdown, ensure_ascii=False),
    )


async def get_pressure_cache(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT pressure_score, breakdown, computed_at FROM infra_pressure_cache WHERE owner_id=$1",
        owner_id,
    )
    if not row:
        return None

    breakdown = row["breakdown"]
    if isinstance(breakdown, str):
        breakdown = json.loads(breakdown)
    return {
        "score": row["pressure_score"],
        "breakdown": breakdown,
        "computed_at": row["computed_at"],
    }


# ── Strike Email Accounts ─────────────────────────────────────────────────────


async def get_strike_email_accounts(
    pool: asyncpg.Pool, owner_id: int
) -> list[asyncpg.Record]:
    """Получить все email-аккаунты пользователя для Strike-репортов."""
    return await pool.fetch(
        """SELECT id, email, smtp_host, smtp_port, is_active, fail_count, last_used_at, added_at,
                  COALESCE(auth_type, 'password') AS auth_type, oauth_provider,
                  oauth_expires_at, oauth_scopes
           FROM strike_email_accounts
           WHERE owner_id=$1
           ORDER BY added_at""",
        owner_id,
    )


async def add_strike_email_account(
    pool: asyncpg.Pool,
    owner_id: int,
    email: str,
    smtp_host: str,
    smtp_port: int,
    smtp_pass: str,
) -> int:
    """Добавить email-аккаунт (или обновить если уже существует). Возвращает id."""
    from services.token_vault import encrypt_token as _enc_tok
    row = await pool.fetchrow(
        """INSERT INTO strike_email_accounts
               (owner_id, email, smtp_host, smtp_port, smtp_pass, auth_type)
            VALUES ($1, $2, $3, $4, $5, 'password')
           ON CONFLICT (owner_id, email)
           DO UPDATE SET smtp_host=$3, smtp_port=$4, smtp_pass=$5,
                         auth_type='password', oauth_provider=NULL,
                         oauth_refresh_token=NULL, oauth_access_token=NULL,
                         oauth_expires_at=NULL, oauth_scopes='{}'::text[],
                         is_active=TRUE, fail_count=0
           RETURNING id""",
        owner_id,
        email,
        smtp_host,
        smtp_port,
        _enc_tok(smtp_pass),
    )
    return row["id"]


async def delete_strike_email_account(
    pool: asyncpg.Pool, owner_id: int, email_id: int
) -> bool:
    """Удалить email-аккаунт. Возвращает True если запись была найдена и удалена."""
    result = await pool.execute(
        "DELETE FROM strike_email_accounts WHERE id=$1 AND owner_id=$2",
        email_id,
        owner_id,
    )
    return result != "DELETE 0"


async def update_strike_email_fail_count(
    pool: asyncpg.Pool, email_id: int, increment: int = 1
) -> None:
    """Увеличить счётчик ошибок для email-аккаунта."""
    await pool.execute(
        """UPDATE strike_email_accounts
           SET fail_count = fail_count + $2,
               last_used_at = NOW()
           WHERE id=$1""",
        email_id,
        increment,
    )


# ── Error Reports ─────────────────────────────────────────────────────────────


async def get_error_reports(
    pool: asyncpg.Pool,
    status: str | None = "new",
    limit: int = 10,
    offset: int = 0,
) -> list[asyncpg.Record]:
    """Получить список отчётов об ошибках для администратора с пагинацией.

    Если status=None — возвращает отчёты всех статусов.
    """
    if status is None:
        rows = await pool.fetch(
            """SELECT er.id, er.user_id, er.description, er.screenshot_id,
                      er.status, er.notes, er.created_at,
                      pu.username, pu.first_name
               FROM error_reports er
               LEFT JOIN platform_users pu ON pu.user_id = er.user_id
               ORDER BY er.created_at DESC
               LIMIT $1 OFFSET $2""",
            limit,
            offset,
        )
    else:
        rows = await pool.fetch(
            """SELECT er.id, er.user_id, er.description, er.screenshot_id,
                      er.status, er.notes, er.created_at,
                      pu.username, pu.first_name
               FROM error_reports er
               LEFT JOIN platform_users pu ON pu.user_id = er.user_id
               WHERE er.status = $1
               ORDER BY er.created_at DESC
               LIMIT $2 OFFSET $3""",
            status,
            limit,
            offset,
        )
    return rows


async def get_error_report(pool: asyncpg.Pool, report_id: int) -> asyncpg.Record | None:
    """Получить один отчёт об ошибке по ID."""
    return await pool.fetchrow(
        """SELECT er.id, er.user_id, er.description, er.screenshot_id,
                  er.status, er.notes, er.assignee_id, er.created_at, er.updated_at,
                  pu.username, pu.first_name
           FROM error_reports er
           LEFT JOIN platform_users pu ON pu.user_id = er.user_id
           WHERE er.id = $1""",
        report_id,
    )


async def update_error_report_status(
    pool: asyncpg.Pool,
    report_id: int,
    status: str,
    notes: str | None = None,
) -> bool:
    """Обновить статус отчёта об ошибке. Возвращает True если запись найдена."""
    result = await pool.execute(
        """UPDATE error_reports
           SET status=$2,
               notes=COALESCE($3, notes),
               updated_at=NOW()
           WHERE id=$1""",
        report_id,
        status,
        notes,
    )
    return result != "UPDATE 0"


# ── BotMother Memory (convenience wrappers over services/ai_memory) ───────────


async def add_memory(
    pool: asyncpg.Pool,
    owner_id: int,
    body: str,
    *,
    title: str = "",
    kind: str = "note",
    tags: list[str] | None = None,
    source: str = "manual",
    pinned: bool = False,
) -> asyncpg.Record:
    """Сохранить запись в памяти BotMother. Возвращает созданную строку."""
    row = await pool.fetchrow(
        """
        INSERT INTO botmother_memory(owner_id, kind, title, body, tags, source, pinned)
        VALUES($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, kind, title, body, tags, pinned, created_at, updated_at
        """,
        owner_id,
        (kind or "note").strip()[:32],
        (title or body[:80]).strip()[:180],
        body.strip()[:8000],
        [t.strip().lower().lstrip("#")[:48] for t in (tags or []) if t.strip()][:12],
        source.strip()[:32],
        pinned,
    )
    return row


async def get_memories(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
    kind: str | None = None,
) -> list[asyncpg.Record]:
    """Получить записи памяти пользователя, отсортированные по pinned DESC, updated_at DESC."""
    if kind:
        rows = await pool.fetch(
            """
            SELECT id, kind, title, body, tags, source, pinned, created_at, updated_at
            FROM botmother_memory
            WHERE owner_id=$1 AND kind=$2
            ORDER BY pinned DESC, updated_at DESC
            LIMIT $3 OFFSET $4
            """,
            owner_id,
            kind,
            limit,
            offset,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, kind, title, body, tags, source, pinned, created_at, updated_at
            FROM botmother_memory
            WHERE owner_id=$1
            ORDER BY pinned DESC, updated_at DESC
            LIMIT $2 OFFSET $3
            """,
            owner_id,
            limit,
            offset,
        )
    return list(rows)


async def delete_memory(
    pool: asyncpg.Pool,
    owner_id: int,
    memory_id: int,
) -> bool:
    """Удалить запись памяти. Возвращает True если запись была найдена и удалена."""
    result = await pool.execute(
        "DELETE FROM botmother_memory WHERE owner_id=$1 AND id=$2",
        owner_id,
        memory_id,
    )
    return result == "DELETE 1"


# ── Ecosystem helpers ──────────────────────────────────────────────────────────


async def get_user_ecosystem_count(pool: asyncpg.Pool, owner_id: int) -> int:
    """Количество активных экосистем пользователя."""
    return (
        await pool.fetchval(
            "SELECT COUNT(*) FROM ecosystems WHERE owner_id=$1 AND status='active'",
            owner_id,
        )
        or 0
    )


async def find_object_ecosystems(
    pool: asyncpg.Pool, owner_id: int, object_type: str, object_id: int
) -> list[dict]:
    """Найти все экосистемы, в которых состоит данный объект."""
    return await pool.fetch(
        """SELECT e.id, e.name, e.ecosystem_type, e.health_score, e.risk_level
           FROM ecosystem_members m
           JOIN ecosystems e ON e.id=m.ecosystem_id
           WHERE m.owner_id=$1 AND m.object_type=$2 AND m.object_id=$3
             AND e.status='active'
           ORDER BY e.name""",
        owner_id,
        object_type,
        object_id,
    )


# ── Intent Engine (v71) ──────────────────────────────────────────────────────


async def create_intent(
    pool: asyncpg.Pool,
    owner_id: int,
    intent_type: str,
    description: str,
    plan: dict,
    strategy: str,
    forecast: dict,
) -> int:

    return await pool.fetchval(
        """INSERT INTO intents (owner_id, intent_type, description, plan, strategy, forecast)
           VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)
           RETURNING id""",
        owner_id,
        intent_type,
        description,
        json.dumps(plan, ensure_ascii=False),
        strategy,
        json.dumps(forecast, ensure_ascii=False),
    )


async def get_intent(
    pool: asyncpg.Pool, intent_id: int, owner_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM intents WHERE id=$1 AND owner_id=$2",
        intent_id,
        owner_id,
    )


async def list_intents(
    pool: asyncpg.Pool, owner_id: int, limit: int = 10
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM intents WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
        owner_id,
        limit,
    )


async def update_intent_strategy(
    pool: asyncpg.Pool, intent_id: int, owner_id: int, strategy: str, forecast: dict
) -> None:

    await pool.execute(
        "UPDATE intents SET strategy=$1, forecast=$2::jsonb WHERE id=$3 AND owner_id=$4",
        strategy,
        json.dumps(forecast, ensure_ascii=False),
        intent_id,
        owner_id,
    )


async def update_intent_status(
    pool: asyncpg.Pool, intent_id: int, owner_id: int, status: str
) -> None:
    ts_col = ""
    if status == "executing":
        ts_col = ", executed_at=NOW()"
    elif status in ("completed", "failed", "cancelled"):
        ts_col = ", completed_at=NOW()"
    await pool.execute(
        f"UPDATE intents SET status=$1{ts_col} WHERE id=$2 AND owner_id=$3",
        status,
        intent_id,
        owner_id,
    )


async def save_intent_feedback(
    pool: asyncpg.Pool, intent_id: int, owner_id: int, feedback: dict
) -> None:

    await pool.execute(
        "UPDATE intents SET feedback=$1::jsonb, status='completed', completed_at=NOW() "
        "WHERE id=$2 AND owner_id=$3",
        json.dumps(feedback, ensure_ascii=False),
        intent_id,
        owner_id,
    )


async def link_intent_operation(pool: asyncpg.Pool, intent_id: int, op_id: int) -> None:
    await pool.execute(
        "INSERT INTO intent_operation_links (intent_id, op_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        intent_id,
        op_id,
    )


async def get_intent_by_op(pool: asyncpg.Pool, op_id: int):
    return await pool.fetchrow(
        "SELECT i.* FROM intents i "
        "JOIN intent_operation_links l ON l.intent_id = i.id "
        "WHERE l.op_id = $1",
        op_id,
    )


# ── Activity Log ──────────────────────────────────────────────────────────────


async def get_activity_feed(
    pool: asyncpg.Pool,
    owner_id: int | None = None,
    status_filter: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> list[asyncpg.Record]:
    """UI events from activity_log, newest first."""
    conditions = []
    params: list = []
    idx = 1
    if owner_id is not None:
        conditions.append(f"owner_id=${idx}")
        params.append(owner_id)
        idx += 1
    if status_filter == "error":
        # Include both 'error' (unhandled crashes) and 'warning' (handled user-visible errors)
        # so the "Только ошибки" view shows all problems, not just uncaught exceptions.
        conditions.append("status IN ('error', 'warning')")
    elif status_filter:
        conditions.append(f"status=${idx}")
        params.append(status_filter)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]
    return await pool.fetch(
        f"""SELECT id, owner_id, event_type, action, detail, status, error_msg,
                   duration_ms, occurred_at
            FROM activity_log
            {where}
            ORDER BY occurred_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *params,
    )


async def get_account_ops_feed(
    pool: asyncpg.Pool,
    owner_id: int | None = None,
    status_filter: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> list[asyncpg.Record]:
    """Account-level operations from operation_audit, newest first."""
    conditions = []
    params: list = []
    idx = 1
    if owner_id is not None:
        conditions.append(f"owner_id=${idx}")
        params.append(owner_id)
        idx += 1
    if status_filter == "error":
        conditions.append("result != 'success'")
    elif status_filter == "ok":
        conditions.append("result = 'success'")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]
    return await pool.fetch(
        f"""SELECT occurred_at, owner_id, action, target, result, error_msg,
                   duration_ms, flood_wait_s, account_id
            FROM operation_audit
            {where}
            ORDER BY occurred_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *params,
    )


async def get_activity_stats(pool: asyncpg.Pool) -> dict:
    """Quick platform-wide stats for admin dashboard."""
    row = await pool.fetchrow(
        """SELECT
            COUNT(*) FILTER (WHERE occurred_at > NOW() - INTERVAL '1 hour')  AS last_hour,
            COUNT(*) FILTER (WHERE occurred_at > NOW() - INTERVAL '24 hours') AS last_day,
            COUNT(*) FILTER (WHERE status='error' AND occurred_at > NOW() - INTERVAL '24 hours') AS errors_day,
            COUNT(DISTINCT owner_id) FILTER (WHERE occurred_at > NOW() - INTERVAL '1 hour') AS active_users_hour
           FROM activity_log"""
    )
    if not row:
        return {"last_hour": 0, "last_day": 0, "errors_day": 0, "active_users_hour": 0}
    return {
        "last_hour": row["last_hour"] or 0,
        "last_day": row["last_day"] or 0,
        "errors_day": row["errors_day"] or 0,
        "active_users_hour": row["active_users_hour"] or 0,
    }


# ── CRM Deals ─────────────────────────────────────────────────────────────────

async def create_crm_deal(
    pool: asyncpg.Pool,
    owner_id: int,
    title: str,
    contact: str = "",
    stage: str = "new",
    value: float = 0.0,
    notes: str = "",
) -> int:
    """Create a new CRM deal. Returns new deal id."""
    row = await pool.fetchrow(
        """INSERT INTO crm_deals(owner_id, title, contact, stage, value, notes)
           VALUES($1,$2,$3,$4,$5,$6)
           RETURNING id""",
        owner_id, title, contact or None, stage, value, notes or None,
    )
    return row["id"]


async def get_crm_deals(
    pool: asyncpg.Pool,
    owner_id: int,
    stage: str | None = None,
) -> list[asyncpg.Record]:
    """Return deals for owner, optionally filtered by stage."""
    if stage:
        return await pool.fetch(
            "SELECT * FROM crm_deals WHERE owner_id=$1 AND stage=$2 ORDER BY updated_at DESC",
            owner_id, stage,
        )
    return await pool.fetch(
        "SELECT * FROM crm_deals WHERE owner_id=$1 ORDER BY updated_at DESC",
        owner_id,
    )


async def get_crm_deal(pool: asyncpg.Pool, deal_id: int, owner_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM crm_deals WHERE id=$1 AND owner_id=$2", deal_id, owner_id
    )


async def move_crm_deal_stage(
    pool: asyncpg.Pool, deal_id: int, owner_id: int, stage: str
) -> None:
    await pool.execute(
        "UPDATE crm_deals SET stage=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
        stage, deal_id, owner_id,
    )


async def delete_crm_deal(pool: asyncpg.Pool, deal_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM crm_deals WHERE id=$1 AND owner_id=$2", deal_id, owner_id
    )


async def add_crm_activity(
    pool: asyncpg.Pool, owner_id: int, deal_id: int, note: str
) -> None:
    await pool.execute(
        "INSERT INTO crm_activity(owner_id, deal_id, note) VALUES($1,$2,$3)",
        owner_id, deal_id, note,
    )


async def get_crm_activity(
    pool: asyncpg.Pool, deal_id: int, limit: int = 10
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM crm_activity WHERE deal_id=$1 ORDER BY created_at DESC LIMIT $2",
        deal_id, limit,
    )


async def get_crm_dashboard_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Return deal counts by stage and total value of won deals."""
    rows = await pool.fetch(
        """SELECT stage, COUNT(*) AS cnt, COALESCE(SUM(value),0) AS total_val
           FROM crm_deals WHERE owner_id=$1 GROUP BY stage""",
        owner_id,
    )
    stats: dict = {s: {"count": 0, "value": 0.0} for s in ("new", "contacted", "qualified", "won", "lost")}
    for r in rows:
        stats[r["stage"]] = {"count": r["cnt"], "value": float(r["total_val"])}
    return stats


# ── SEO Score History ─────────────────────────────────────────────────────────

async def save_seo_score(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_type: str,
    entity_id: int,
    score: int,
    tips: list[str] | None = None,
) -> None:
    """Persist an SEO check result."""
    import json as _json
    tips_json = _json.dumps(tips or [], ensure_ascii=False)
    try:
        await pool.execute(
            """INSERT INTO seo_score_history(owner_id, entity_type, entity_id, score, tips_json)
               VALUES($1,$2,$3,$4,$5)""",
            owner_id, entity_type, entity_id, score, tips_json,
        )
    except Exception:
        pass  # non-critical


async def get_seo_score_history(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_type: str,
    entity_id: int,
    limit: int = 10,
) -> list[asyncpg.Record]:
    """Return last N SEO checks for a given entity."""
    try:
        return await pool.fetch(
            """SELECT score, tips_json, checked_at
               FROM seo_score_history
               WHERE owner_id=$1 AND entity_type=$2 AND entity_id=$3
               ORDER BY checked_at DESC LIMIT $4""",
            owner_id, entity_type, entity_id, limit,
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Infrastructure-as-Radar: track all entities seen across client sessions
# ---------------------------------------------------------------------------

async def record_entity_sighting(
    pool: asyncpg.Pool,
    entity_id: int,
    entity_type: str,
    chat_id: int | None = None,
) -> None:
    """
    Фиксирует факт встречи с Telegram-сущностью в рамках нашей инфраструктуры.

    Вызывается при каждом парсинге участников чата, при анализе пользователя
    или канала. Обновляет две таблицы: seen_entities (детали каждой встречи)
    и entity_radar_stats (агрегат для быстрого чтения).

    Параметры:
        pool        — asyncpg.Pool, пул соединений к базе данных
        entity_id   — числовой Telegram ID пользователя/канала/группы/бота
        entity_type — строка: 'user' | 'bot' | 'channel' | 'group' | 'supergroup'
        chat_id     — ID чата, где встретили сущность; None если прямой lookup
                      (хранится как 0 внутри для PK-совместимости — chat_id=0
                      означает «прямой поиск вне чата»)

    Возвращает: None. Все ошибки молча поглощаются — fire-and-forget семантика.

    Побочные эффекты:
        seen_entities: UPSERT по (entity_id, chat_id), инкрементирует sighting_count,
            обновляет last_seen_at; при первом встречании устанавливает first_seen_at
        entity_radar_stats: полный пересчёт агрегатов через SELECT...FROM seen_entities
            — first_seen_at, last_seen_at, distinct_chats (без учёта chat_id=0),
            total_sightings; updated_at всегда обновляется до NOW()

    Граничные случаи:
        — chat_id=None и chat_id=0 обрабатываются одинаково (прямой lookup)
        — При сетевых ошибках или constraint violation исключение поглощается;
          вызывающий код не получает никакого сигнала об ошибке
        — entity_type при конфликте в entity_radar_stats перезаписывается последним
          переданным значением (EXCLUDED.entity_type)
    """
    chat_key = chat_id if chat_id is not None else 0
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO seen_entities
                       (entity_id, entity_type, chat_id, seen_at, first_seen_at, last_seen_at, sighting_count)
                   VALUES ($1, $2, $3, NOW(), NOW(), NOW(), 1)
                   ON CONFLICT (entity_id, chat_id) DO UPDATE
                       SET last_seen_at  = NOW(),
                           sighting_count = seen_entities.sighting_count + 1""",
                entity_id, entity_type, chat_key,
            )
            await conn.execute(
                """INSERT INTO entity_radar_stats
                       (entity_id, entity_type, first_seen_at, last_seen_at, distinct_chats, total_sightings, updated_at)
                   SELECT
                       $1,
                       $2,
                       MIN(first_seen_at),
                       MAX(last_seen_at),
                       COUNT(DISTINCT NULLIF(chat_id, 0)),
                       SUM(sighting_count),
                       NOW()
                   FROM seen_entities WHERE entity_id = $1
                   ON CONFLICT (entity_id) DO UPDATE
                       SET entity_type    = EXCLUDED.entity_type,
                           first_seen_at  = EXCLUDED.first_seen_at,
                           last_seen_at   = EXCLUDED.last_seen_at,
                           distinct_chats = EXCLUDED.distinct_chats,
                           total_sightings= EXCLUDED.total_sightings,
                           updated_at     = NOW()""",
                entity_id, entity_type,
            )
    except Exception:
        pass


async def get_entity_radar_stats(
    pool: asyncpg.Pool,
    entity_id: int,
) -> dict:
    """
    Return radar stats for entity_id: first_seen, last_seen, distinct_chats, total_sightings.
    Returns empty dict if no data.
    """
    try:
        row = await pool.fetchrow(
            """SELECT first_seen_at, last_seen_at, distinct_chats, total_sightings
               FROM entity_radar_stats WHERE entity_id = $1""",
            entity_id,
        )
        if row:
            return dict(row)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Username / display-name history tracking
# ---------------------------------------------------------------------------

async def record_name_snapshot(
    pool: asyncpg.Pool,
    entity_id: int,
    entity_type: str,
    username: str | None,
    display_name: str | None,
) -> bool:
    """
    Check if username or display_name changed since last observation.
    If yes — insert a new record into entity_name_history and update entity_last_known.
    Returns True if a change was detected and recorded.
    Fire-and-forget safe: swallows all errors.
    """
    try:
        async with pool.acquire() as conn:
            last = await conn.fetchrow(
                "SELECT username, display_name FROM entity_last_known WHERE entity_id=$1",
                entity_id,
            )
            changed = (
                last is None
                or last["username"] != username
                or last["display_name"] != display_name
            )
            if changed:
                await conn.execute(
                    """INSERT INTO entity_name_history (entity_id, entity_type, username, display_name, seen_at)
                       VALUES ($1, $2, $3, $4, NOW())""",
                    entity_id, entity_type, username, display_name,
                )
            # Always refresh last_seen_at
            await conn.execute(
                """INSERT INTO entity_last_known
                       (entity_id, entity_type, username, display_name, first_seen_at, last_seen_at)
                   VALUES ($1, $2, $3, $4, NOW(), NOW())
                   ON CONFLICT (entity_id) DO UPDATE
                       SET entity_type   = EXCLUDED.entity_type,
                           username      = EXCLUDED.username,
                           display_name  = EXCLUDED.display_name,
                           last_seen_at  = NOW()""",
                entity_id, entity_type, username, display_name,
            )
            return changed
    except Exception:
        return False


async def get_name_history(
    pool: asyncpg.Pool,
    entity_id: int,
    limit: int = 15,
) -> list[asyncpg.Record]:
    """
    Return chronological list of (username, display_name, seen_at) for entity.
    Oldest first. Includes all distinct changes.
    """
    try:
        return await pool.fetch(
            """SELECT username, display_name, seen_at
               FROM entity_name_history
               WHERE entity_id=$1
               ORDER BY seen_at ASC
               LIMIT $2""",
            entity_id, limit,
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entity follow / watch list
# ---------------------------------------------------------------------------

async def follow_entity(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_id: int,
    entity_type: str,
    label: str | None = None,
) -> bool:
    """Add entity to owner's follow list. Returns True if newly added, False if already following."""
    try:
        result = await pool.execute(
            """INSERT INTO entity_follows (owner_id, entity_id, entity_type, label)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (owner_id, entity_id) DO NOTHING""",
            owner_id, entity_id, entity_type, label,
        )
        return "INSERT 0 1" in str(result)
    except Exception:
        return False


async def unfollow_entity(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_id: int,
) -> bool:
    """Remove entity from owner's follow list. Returns True if removed."""
    try:
        result = await pool.execute(
            "DELETE FROM entity_follows WHERE owner_id=$1 AND entity_id=$2",
            owner_id, entity_id,
        )
        return "DELETE 1" in str(result)
    except Exception:
        return False


async def is_following(pool: asyncpg.Pool, owner_id: int, entity_id: int) -> bool:
    """Check if owner is following entity."""
    try:
        row = await pool.fetchrow(
            "SELECT id FROM entity_follows WHERE owner_id=$1 AND entity_id=$2",
            owner_id, entity_id,
        )
        return row is not None
    except Exception:
        return False


async def get_follows(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Return all entities owner is following, newest first."""
    try:
        return await pool.fetch(
            """SELECT f.id, f.entity_id, f.entity_type, f.label, f.created_at, f.last_checked_at,
                      lk.username, lk.display_name
               FROM entity_follows f
               LEFT JOIN entity_last_known lk ON lk.entity_id = f.entity_id
               WHERE f.owner_id = $1
               ORDER BY f.created_at DESC
               LIMIT $2""",
            owner_id, limit,
        )
    except Exception:
        return []


async def record_follow_change(
    pool: asyncpg.Pool,
    follow_id: int,
    owner_id: int,
    entity_id: int,
    change_type: str,
    old_username: str | None,
    new_username: str | None,
    old_name: str | None,
    new_name: str | None,
) -> int | None:
    """Record a detected change for a followed entity. Returns new event id."""
    try:
        return await pool.fetchval(
            """INSERT INTO entity_follow_events
                   (follow_id, owner_id, entity_id, change_type,
                    old_username, new_username, old_name, new_name)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               RETURNING id""",
            follow_id, owner_id, entity_id, change_type,
            old_username, new_username, old_name, new_name,
        )
    except Exception:
        return None


async def get_pending_follow_notifications(
    pool: asyncpg.Pool,
    limit: int = 100,
) -> list[asyncpg.Record]:
    """Return unnotified follow events for delivery."""
    try:
        return await pool.fetch(
            """SELECT e.id, e.follow_id, e.owner_id, e.entity_id,
                      e.change_type, e.old_username, e.new_username,
                      e.old_name, e.new_name, e.detected_at
               FROM entity_follow_events e
               WHERE NOT e.notified
               ORDER BY e.detected_at ASC
               LIMIT $1""",
            limit,
        )
    except Exception:
        return []


async def mark_follow_notifications_sent(
    pool: asyncpg.Pool,
    event_ids: list[int],
) -> None:
    """Mark events as notified."""
    if not event_ids:
        return
    try:
        await pool.execute(
            "UPDATE entity_follow_events SET notified=TRUE WHERE id = ANY($1::bigint[])",
            event_ids,
        )
    except Exception:
        pass


# ── Subscription Gate ─────────────────────────────────────────────────────────

async def get_subscription_gate_channels(pool: asyncpg.Pool) -> list:
    return await pool.fetch(
        "SELECT id, channel_username, channel_title, added_at FROM subscription_gate_channels ORDER BY id"
    )


async def add_subscription_gate_channel(
    pool: asyncpg.Pool, username: str, title: str = ""
) -> int:
    row = await pool.fetchrow(
        "INSERT INTO subscription_gate_channels (channel_username, channel_title) VALUES ($1, $2) RETURNING id",
        username,
        title,
    )
    return row["id"]


async def remove_subscription_gate_channel(pool: asyncpg.Pool, channel_id: int) -> None:
    await pool.execute("DELETE FROM subscription_gate_channels WHERE id=$1", channel_id)


# ── Promo Platform (v99) ───────────────────────────────────────────────────────

async def promo_create_order(
    pool: asyncpg.Pool,
    owner_id: int,
    keyword: str,
    target_position: int = 1,
    bot_id: int | None = None,
    smm_panel_id: int | None = None,
    target_subs: int | None = None,
) -> int:
    """Создать заказ продвижения.

    bot_id и smm_panel_id приходят из данных кнопки, то есть от клиента.
    Чужой номер панели, попав в заказ, потом превращается в запуск накрутки по
    чужому API-ключу и за чужой счёт, поэтому принадлежность проверяется здесь,
    при записи: дальше по коду заказ уже считается согласованным.
    """
    if bot_id is not None and not await warehouse_get_bot(pool, bot_id, owner_id=owner_id):
        raise ValueError("Бот не найден в складе этого пользователя")
    if smm_panel_id is not None and not await smm_get_panel(pool, smm_panel_id, owner_id=owner_id):
        raise ValueError("SMM-панель не принадлежит этому пользователю")
    row = await pool.fetchrow(
        """INSERT INTO promo_orders
               (owner_id, keyword, target_position, bot_id, smm_panel_id, target_subs)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        owner_id, keyword, target_position, bot_id, smm_panel_id, target_subs,
    )
    return row["id"]


async def promo_get_order(
    pool: asyncpg.Pool, order_id: int, owner_id: int | None = None
) -> asyncpg.Record | None:
    """Заказ продвижения. С owner_id — только свой, иначе None.

    owner_id необязателен ради планировщика, который обходит заказы всех
    владельцев. Любой путь, где номер заказа пришёл от человека (кнопка,
    запрос Mini App), обязан его передавать.
    """
    if owner_id is not None:
        return await pool.fetchrow(
            "SELECT * FROM promo_orders WHERE id=$1 AND owner_id=$2",
            order_id, owner_id,
        )
    return await pool.fetchrow(
        "SELECT * FROM promo_orders WHERE id=$1", order_id
    )


async def promo_list_orders(
    pool: asyncpg.Pool, owner_id: int, status: str | None = None, limit: int = 20, offset: int = 0
) -> list[asyncpg.Record]:
    if status:
        return await pool.fetch(
            "SELECT * FROM promo_orders WHERE owner_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3 OFFSET $4",
            owner_id, status, limit, offset,
        )
    return await pool.fetch(
        "SELECT * FROM promo_orders WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        owner_id, limit, offset,
    )


async def promo_update_order_status(
    pool: asyncpg.Pool, order_id: int, status: str, **kwargs
) -> None:
    sets = ["status=$2", "updated_at=NOW()"]
    vals: list = [order_id, status]
    idx = 3
    allowed = {"smm_order_id", "last_position", "current_subs", "smm_panel_id", "completed_at"}
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=${idx}")
            vals.append(v)
            idx += 1
    await pool.execute(
        f"UPDATE promo_orders SET {', '.join(sets)} WHERE id=$1",
        *vals,
    )


async def promo_delete_order(pool: asyncpg.Pool, order_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM promo_orders WHERE id=$1 AND owner_id=$2", order_id, owner_id
    )


# ── Bot Warehouse ─────────────────────────────────────────────────────────────

async def warehouse_add_bot(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_username: str,
    bot_token_enc: str | None = None,
    session_path: str | None = None,
    account_id: int | None = None,
    proxy_id: int | None = None,
    registered_at=None,
    notes: str = "",
) -> int:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc)
    if registered_at is None:
        registered_at = now
    ready_at = registered_at + timedelta(days=21)
    status = "ready" if now >= ready_at else "aging"
    row = await pool.fetchrow(
        """INSERT INTO bot_warehouse
               (owner_id, bot_username, bot_token_enc, session_path,
                account_id, proxy_id, status, registered_at, ready_at, notes)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING id""",
        owner_id, bot_username.lstrip("@"), bot_token_enc, session_path,
        account_id, proxy_id, status, registered_at, ready_at, notes,
    )
    return row["id"]


async def warehouse_get_bot(
    pool: asyncpg.Pool, bot_id: int, owner_id: int | None = None
) -> asyncpg.Record | None:
    """Бот со склада. С owner_id — только свой, иначе None.

    Строка содержит bot_token_enc и путь к файлу сессии, то есть полный доступ
    к боту. Отдавать её по одному номеру нельзя.
    """
    if owner_id is not None:
        return await pool.fetchrow(
            "SELECT * FROM bot_warehouse WHERE id=$1 AND owner_id=$2",
            bot_id, owner_id,
        )
    return await pool.fetchrow("SELECT * FROM bot_warehouse WHERE id=$1", bot_id)


async def warehouse_list_bots(
    pool: asyncpg.Pool, owner_id: int, status: str | None = None, limit: int = 20, offset: int = 0
) -> list[asyncpg.Record]:
    if status:
        return await pool.fetch(
            "SELECT * FROM bot_warehouse WHERE owner_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3 OFFSET $4",
            owner_id, status, limit, offset,
        )
    return await pool.fetch(
        "SELECT * FROM bot_warehouse WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        owner_id, limit, offset,
    )


async def warehouse_update_bot(
    pool: asyncpg.Pool, bot_id: int, owner_id: int, **kwargs
) -> None:
    allowed = {"status", "current_subs", "session_path", "account_id", "proxy_id", "notes", "bot_token_enc"}
    sets = ["updated_at=NOW()"]
    vals: list = [bot_id, owner_id]
    idx = 3
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=${idx}")
            vals.append(v)
            idx += 1
    if len(vals) == 2:
        return
    await pool.execute(
        f"UPDATE bot_warehouse SET {', '.join(sets)} WHERE id=$1 AND owner_id=$2",
        *vals,
    )


async def warehouse_delete_bot(pool: asyncpg.Pool, bot_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM bot_warehouse WHERE id=$1 AND owner_id=$2", bot_id, owner_id
    )


async def warehouse_refresh_statuses(pool: asyncpg.Pool) -> int:
    """Update aging→ready for bots where ready_at <= NOW(). Returns count updated."""
    result = await pool.execute(
        "UPDATE bot_warehouse SET status='ready', updated_at=NOW() WHERE status='aging' AND ready_at <= NOW()"
    )
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


# ── SMM Panels ────────────────────────────────────────────────────────────────

async def smm_add_panel(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    api_url: str,
    api_key_enc: str,
    service_id: str = "",
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO smm_panels (owner_id, name, api_url, api_key_enc, service_id)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        owner_id, name, api_url, api_key_enc, service_id,
    )
    return row["id"]


async def smm_list_panels(
    pool: asyncpg.Pool, owner_id: int
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM smm_panels WHERE owner_id=$1 ORDER BY created_at DESC",
        owner_id,
    )


# ── Booster Sessions (v99) ─────────────────────────────────────────────────────

async def bb_get_sessions(pool: asyncpg.Pool, owner_id: int) -> list:
    rows = await pool.fetch(
        "SELECT id, phone, label, status, proxy, last_used_at, added_at FROM booster_sessions WHERE owner_id=$1 ORDER BY added_at DESC",
        owner_id,
    )
    # decrypt proxy для отображения (passthrough legacy-plaintext)
    from services.token_vault import decrypt_token

    out = []
    for r in rows:
        d = dict(r)
        if d.get("proxy"):
            d["proxy"] = decrypt_token(d["proxy"])
        out.append(d)
    return out


async def smm_get_panel(
    pool: asyncpg.Pool, panel_id: int, owner_id: int | None = None
) -> asyncpg.Record | None:
    """SMM-панель. С owner_id — только своя, иначе None.

    В строке лежит api_key_enc — ключ к платному счёту владельца. Запуск
    накрутки по чужой панели тратит чужие деньги, поэтому все пути от человека
    обязаны передавать owner_id.
    """
    if owner_id is not None:
        return await pool.fetchrow(
            "SELECT * FROM smm_panels WHERE id=$1 AND owner_id=$2",
            panel_id, owner_id,
        )
    return await pool.fetchrow("SELECT * FROM smm_panels WHERE id=$1", panel_id)


async def smm_update_panel(
    pool: asyncpg.Pool, panel_id: int, owner_id: int, **kwargs
) -> None:
    allowed = {"name", "api_url", "api_key_enc", "service_id", "is_active", "balance", "last_checked"}
    sets: list = []
    vals: list = [panel_id, owner_id]
    idx = 3
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=${idx}")
            vals.append(v)
            idx += 1
    if not sets:
        return
    await pool.execute(
        f"UPDATE smm_panels SET {', '.join(sets)} WHERE id=$1 AND owner_id=$2",
        *vals,
    )


async def bb_add_session(pool: asyncpg.Pool, owner_id: int, phone: str, session_str: str, proxy: str | None, label: str | None) -> int:
    # Шифруем секреты at-rest (session_str = доступ к аккаунту; proxy = креды).
    from services.token_vault import encrypt_token

    row = await pool.fetchrow(
        "INSERT INTO booster_sessions(owner_id,phone,session_str,proxy,label) VALUES($1,$2,$3,$4,$5) RETURNING id",
        owner_id, phone, encrypt_token(session_str),
        encrypt_token(proxy) if proxy else proxy, label,
    )
    return row["id"]


async def bb_get_session(pool: asyncpg.Pool, session_id: int, owner_id: int):
    row = await pool.fetchrow(
        "SELECT * FROM booster_sessions WHERE id=$1 AND owner_id=$2", session_id, owner_id
    )
    if not row:
        return row
    # decrypt секретов для потребителя (passthrough legacy-plaintext)
    from services.token_vault import decrypt_token

    d = dict(row)
    if d.get("session_str"):
        d["session_str"] = decrypt_token(d["session_str"])
    if d.get("proxy"):
        d["proxy"] = decrypt_token(d["proxy"])
    return d


async def bb_delete_session(pool: asyncpg.Pool, session_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM booster_sessions WHERE id=$1 AND owner_id=$2", session_id, owner_id
    )


async def bb_get_session_str(pool: asyncpg.Pool, session_id: int, owner_id: int) -> tuple:
    row = await pool.fetchrow(
        "SELECT session_str, proxy FROM booster_sessions WHERE id=$1 AND owner_id=$2 AND status='active'",
        session_id, owner_id,
    )
    if not row:
        return None, None
    # decrypt секретов для потребителя (passthrough legacy-plaintext)
    from services.token_vault import decrypt_token

    _proxy = row["proxy"]
    return decrypt_token(row["session_str"]), (decrypt_token(_proxy) if _proxy else _proxy)


async def smm_delete_panel(pool: asyncpg.Pool, panel_id: int, owner_id: int) -> None:
    await pool.execute("DELETE FROM smm_panels WHERE id=$1 AND owner_id=$2", panel_id, owner_id)


# ── Promo Logs ────────────────────────────────────────────────────────────────

async def promo_log(
    pool: asyncpg.Pool,
    owner_id: int,
    event: str,
    message: str,
    level: str = "INFO",
    order_id: int | None = None,
    meta: dict | None = None,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO promo_logs (order_id, owner_id, level, event, message, meta)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            order_id, owner_id, level, event, message,
            json.dumps(meta or {}),
        )
    except Exception:
        pass


async def promo_get_logs(
    pool: asyncpg.Pool,
    owner_id: int,
    order_id: int | None = None,
    level: str | None = None,
    limit: int = 50,
) -> list[asyncpg.Record]:
    conditions = ["owner_id=$1"]
    vals: list = [owner_id]
    idx = 2
    if order_id is not None:
        conditions.append(f"order_id=${idx}")
        vals.append(order_id)
        idx += 1
    if level:
        conditions.append(f"level=${idx}")
        vals.append(level)
        idx += 1
    where = " AND ".join(conditions)
    vals.append(limit)
    return await pool.fetch(
        f"SELECT * FROM promo_logs WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
        *vals,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GROWTH ENGINE — амбассадоры, комиссии, контент-сидер
# ══════════════════════════════════════════════════════════════════════════════

async def get_growth_platform_stats(pool: asyncpg.Pool) -> dict:
    """Агрегированная статистика платформы для контент-пакетов."""
    row = await pool.fetchrow(
        """SELECT
            (SELECT COUNT(*) FROM platform_users)                            AS total_users,
            (SELECT COUNT(*) FROM operation_queue WHERE status='done')       AS total_ops,
            (SELECT COUNT(*) FROM managed_channels)                          AS total_channels,
            (SELECT COUNT(*) FROM platform_users
             WHERE COALESCE(registered_at, first_seen) >= now() - INTERVAL '7 days') AS week_users
        """
    )
    return dict(row) if row else {"total_users": 0, "total_ops": 0, "total_channels": 0, "week_users": 0}


async def get_ambassador_status(pool: asyncpg.Pool, user_id: int) -> dict:
    """Возвращает текущий тир, реф-статистику и баланс комиссии пользователя."""
    ref_stats = await get_referral_stats(pool, user_id)
    tiers = await pool.fetch(
        "SELECT * FROM ambassador_tiers ORDER BY sort_order"
    )
    # Определяем текущий тир
    current_tier = None
    next_tier = None
    for tier in tiers:
        qualifies = True
        if tier["min_active_refs"] > 0 and ref_stats["active"] < tier["min_active_refs"]:
            qualifies = False
        if tier["min_paid_refs"] > 0 and ref_stats["paid"] < tier["min_paid_refs"]:
            qualifies = False
        if qualifies:
            current_tier = dict(tier)
        elif next_tier is None:
            next_tier = dict(tier)

    commission_balance = await pool.fetchval(
        """SELECT COALESCE(SUM(commission_usd),0) FROM commission_ledger
           WHERE referrer_id=$1 AND status='pending'""",
        user_id,
    ) or 0.0
    paid_out = await pool.fetchval(
        """SELECT COALESCE(SUM(commission_usd),0) FROM commission_ledger
           WHERE referrer_id=$1 AND status='paid'""",
        user_id,
    ) or 0.0
    return {
        "ref_stats": ref_stats,
        "current_tier": current_tier,
        "next_tier": next_tier,
        "tiers": [dict(t) for t in tiers],
        "commission_balance": float(commission_balance),
        "paid_out": float(paid_out),
    }


async def record_commission(
    pool: asyncpg.Pool,
    referrer_id: int,
    referred_id: int,
    payment_amount: float,
    commission_pct: float,
) -> float:
    """Начисляет комиссию реферера. Возвращает сумму начисления."""
    commission_usd = round(payment_amount * commission_pct / 100, 4)
    if commission_usd <= 0:
        return 0.0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO commission_ledger(referrer_id, referred_id, payment_amount,
                       commission_pct, commission_usd)
                   VALUES($1,$2,$3,$4,$5)""",
                referrer_id, referred_id, payment_amount, commission_pct, commission_usd,
            )
            await conn.execute(
                """INSERT INTO ambassador_status(user_id, total_commission)
                   VALUES($1,$2)
                   ON CONFLICT(user_id) DO UPDATE
                   SET total_commission = ambassador_status.total_commission + $2,
                       updated_at = NOW()""",
                referrer_id, commission_usd,
            )
    return commission_usd


async def create_payout_request(
    pool: asyncpg.Pool, user_id: int, amount_usd: float, method: str, wallet: str
) -> int:
    """Создаёт заявку на выплату. Возвращает id запроса."""
    return await pool.fetchval(
        """INSERT INTO payout_requests(user_id, amount_usd, method, wallet)
           VALUES($1,$2,$3,$4) RETURNING id""",
        user_id, amount_usd, method, wallet,
    )


async def get_referral_leaderboard_monthly(pool: asyncpg.Pool, limit: int = 10) -> list[dict]:
    """Топ рефереров за текущий месяц (по платящим рефералам)."""
    rows = await pool.fetch(
        """SELECT pr.referrer_id,
                  COALESCE(pu.first_name, 'Аноним') AS name,
                  pu.username,
                  COUNT(*) FILTER (WHERE pr.paid_at IS NOT NULL)    AS paid_count,
                  COUNT(*) FILTER (WHERE pr.activated_at IS NOT NULL) AS active_count,
                  COUNT(*) AS total_count
           FROM platform_referrals pr
           LEFT JOIN platform_users pu ON pu.user_id = pr.referrer_id
           WHERE pr.created_at >= date_trunc('month', now())
           GROUP BY pr.referrer_id, pu.first_name, pu.username
           ORDER BY paid_count DESC, active_count DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def get_growth_content_seeds(pool: asyncpg.Pool) -> list[dict]:
    """Возвращает активные контент-пакеты."""
    rows = await pool.fetch(
        "SELECT * FROM growth_content_seeds WHERE is_active ORDER BY id"
    )
    return [dict(r) for r in rows]


async def get_commission_history(pool: asyncpg.Pool, user_id: int, limit: int = 20) -> list[dict]:
    """История начислений комиссии."""
    rows = await pool.fetch(
        """SELECT c.id, c.payment_amount, c.commission_pct, c.commission_usd,
                  c.status, c.created_at,
                  COALESCE(pu.first_name, 'Пользователь') AS ref_name
           FROM commission_ledger c
           LEFT JOIN platform_users pu ON pu.user_id = c.referred_id
           WHERE c.referrer_id=$1
           ORDER BY c.created_at DESC LIMIT $2""",
        user_id, limit,
    )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# GROWTH ENGINE v2 — расписание, watermark-настройки, outreach
# ══════════════════════════════════════════════════════════════════════════════

async def get_growth_settings(pool: asyncpg.Pool, user_id: int) -> dict:
    row = await pool.fetchrow("SELECT * FROM growth_settings WHERE user_id=$1", user_id)
    return dict(row) if row else {"user_id": user_id, "watermark_enabled": False}


async def set_watermark_enabled(pool: asyncpg.Pool, user_id: int, enabled: bool) -> None:
    await pool.execute(
        """INSERT INTO growth_settings(user_id, watermark_enabled)
           VALUES($1,$2)
           ON CONFLICT(user_id) DO UPDATE
           SET watermark_enabled=$2, updated_at=NOW()""",
        user_id, enabled,
    )


async def get_user_growth_schedules(pool: asyncpg.Pool, user_id: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT gs.*, gc.title AS seed_title, gc.content_type
           FROM growth_schedule gs
           LEFT JOIN growth_content_seeds gc ON gc.id = gs.seed_id
           WHERE gs.user_id=$1
           ORDER BY gs.created_at DESC""",
        user_id,
    )
    return [dict(r) for r in rows]


async def create_growth_schedule(
    pool: asyncpg.Pool, user_id: int, seed_id: int, interval_h: int
) -> int:
    return await pool.fetchval(
        """INSERT INTO growth_schedule(user_id, seed_id, interval_h,
               next_run_at)
           VALUES($1,$2,$3, NOW() + ($4 || ' hours')::INTERVAL)
           RETURNING id""",
        user_id, seed_id, interval_h, str(interval_h),
    )


async def toggle_growth_schedule(
    pool: asyncpg.Pool, schedule_id: int, user_id: int
) -> bool:
    row = await pool.fetchrow(
        """UPDATE growth_schedule SET is_active = NOT is_active
           WHERE id=$1 AND user_id=$2 RETURNING is_active""",
        schedule_id, user_id,
    )
    return bool(row["is_active"]) if row else False


async def delete_growth_schedule(pool: asyncpg.Pool, schedule_id: int, user_id: int) -> None:
    await pool.execute(
        "DELETE FROM growth_schedule WHERE id=$1 AND user_id=$2", schedule_id, user_id
    )


async def get_due_growth_schedules(pool: asyncpg.Pool) -> list[dict]:
    """Возвращает все активные расписания у которых next_run_at <= NOW()."""
    rows = await pool.fetch(
        """SELECT gs.*, gc.template, gc.content_type
           FROM growth_schedule gs
           JOIN growth_content_seeds gc ON gc.id = gs.seed_id
           WHERE gs.is_active = TRUE AND gs.next_run_at <= NOW()
           FOR UPDATE SKIP LOCKED""",
    )
    return [dict(r) for r in rows]


async def advance_growth_schedule(pool: asyncpg.Pool, schedule_id: int, interval_h: int, sent: int) -> None:
    await pool.execute(
        """UPDATE growth_schedule
           SET last_run_at = NOW(),
               next_run_at = NOW() + ($2 || ' hours')::INTERVAL,
               total_sent  = total_sent + $3
           WHERE id = $1""",
        schedule_id, str(interval_h), sent,
    )


# ── Anti-abuse: phone → owner linking (schema_v124) ──────────────────────────


async def record_phone_link(
    pool: asyncpg.Pool,
    phone: str,
    owner_id: int,
) -> list[int]:
    """Записать связь телефон→владелец и вернуть других владельцев с тем же телефоном.

    При первом обнаружении общего телефона автоматически создаёт запись в
    linked_platform_users для использования в проверках лимитов.
    """
    if not phone:
        return []

    try:
        await pool.execute(
            "INSERT INTO phone_owner_links (phone, owner_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            phone, owner_id,
        )
        rows = await pool.fetch(
            "SELECT owner_id FROM phone_owner_links WHERE phone=$1 AND owner_id!=$2",
            phone, owner_id,
        )
        other_ids = [r["owner_id"] for r in rows]
        for other_id in other_ids:
            a, b = min(owner_id, other_id), max(owner_id, other_id)
            await pool.execute(
                """INSERT INTO linked_platform_users (owner_id_a, owner_id_b, link_type)
                   VALUES ($1,$2,'shared_phone') ON CONFLICT DO NOTHING""",
                a, b,
            )
        return other_ids
    except Exception:
        return []


async def get_linked_owner_ids(pool: asyncpg.Pool, owner_id: int) -> list[int]:
    """Вернуть список owner_id, связанных с данным (через общие телефоны)."""
    try:
        rows = await pool.fetch(
            """SELECT CASE WHEN owner_id_a=$1 THEN owner_id_b ELSE owner_id_a END AS lid
               FROM linked_platform_users
               WHERE owner_id_a=$1 OR owner_id_b=$1""",
            owner_id,
        )
        return [r["lid"] for r in rows]
    except Exception:
        return []


async def count_bots_across_linked(pool: asyncpg.Pool, owner_id: int) -> int:
    """Суммарное количество управляемых ботов у данного владельца + всех связанных."""
    linked = await get_linked_owner_ids(pool, owner_id)
    all_ids = [owner_id] + linked
    try:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by = ANY($1::bigint[]) AND is_active=TRUE",
            all_ids,
        ) or 0
    except Exception:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
            owner_id,
        ) or 0


async def count_channels_across_linked(pool: asyncpg.Pool, owner_id: int) -> int:
    """Суммарное количество управляемых каналов у данного владельца + всех связанных."""
    linked = await get_linked_owner_ids(pool, owner_id)
    all_ids = [owner_id] + linked
    try:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id = ANY($1::bigint[])",
            all_ids,
        ) or 0
    except Exception:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
            owner_id,
        ) or 0


# ── Connection Pool Health Monitor (мониторинг пула соединений) ─────────────
# Автоматический мониторинг состояния пула соединений.

_pool_stats: dict = {
    "total_connections": 0,
    "active_connections": 0,
    "idle_connections": 0,
    "waiting": 0,
    "errors": 0,
    "last_check": 0,
}


async def monitor_pool_health(pool: asyncpg.Pool) -> dict:
    """Monitor connection pool health and return statistics."""
    try:
        # Get pool size info
        _pool_stats["total_connections"] = pool.get_size()
        _pool_stats["idle_connections"] = pool.get_idle_size()
        _pool_stats["active_connections"] = pool.get_size() - pool.get_idle_size()
        _pool_stats["last_check"] = time.time()
        # Метрика (аудит №6): насыщение пула. 20 соединений делят все фоновые
        # циклы и весь HTTP; когда active упирается в total, тормозит ВСЁ сразу,
        # и раньше это было невидимо до жалобы.
        try:
            from services import metrics as _m
            _m.gauge("infragram_db_pool_connections",
                     _pool_stats["total_connections"], {"state": "total"})
            _m.gauge("infragram_db_pool_connections",
                     _pool_stats["active_connections"], {"state": "active"})
            _m.gauge("infragram_db_pool_connections",
                     _pool_stats["idle_connections"], {"state": "idle"})
        except Exception:
            pass
        
        # Check for connection errors
        try:
            async with pool.acquire(timeout=5) as conn:
                await conn.fetchval("SELECT 1")
        except Exception as e:
            _pool_stats["errors"] += 1
            log.warning("pool_health: connection test failed: %s", e)
        
        return {
            "status": "healthy" if _pool_stats["errors"] < 5 else "degraded",
            "total": _pool_stats["total_connections"],
            "active": _pool_stats["active_connections"],
            "idle": _pool_stats["idle_connections"],
            "errors": _pool_stats["errors"],
            "utilization": round(
                _pool_stats["active_connections"] / max(_pool_stats["total_connections"], 1) * 100,
                1,
            ),
        }
    except Exception as e:
        log.warning("pool_health monitoring failed: %s", e)
        return {"status": "error", "error": str(e)}


async def run_pool_monitor(pool: asyncpg.Pool) -> None:
    """Background service: monitor pool health every 5 minutes."""
    log.info("pool_monitor: starting")
    while True:
        try:
            stats = await monitor_pool_health(pool)
            if stats.get("utilization", 0) > 80:
                log.warning(
                    "pool_monitor: high utilization %d%% (active=%d, total=%d)",
                    stats["utilization"],
                    stats.get("active", 0),
                    stats.get("total", 0),
                )
        except Exception as e:
            log.error("pool_monitor: error: %s", e)
        await asyncio.sleep(300)  # каждые 5 минут


# ── Auto-Registrar: saved device-emulation preference ("Генератор параметров") ──


async def get_autoreg_device_profile(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    """Owner's saved manufacturer/app_version pin for Auto-Registrar, or None
    if unset (fully random device pool, the default)."""
    row = await pool.fetchrow(
        "SELECT manufacturer, app_version FROM autoreg_device_profiles WHERE owner_id=$1",
        owner_id,
    )
    return dict(row) if row else None


async def set_autoreg_device_profile(
    pool: asyncpg.Pool, owner_id: int, manufacturer: str | None, app_version: str | None
) -> None:
    await pool.execute(
        """INSERT INTO autoreg_device_profiles(owner_id, manufacturer, app_version)
           VALUES ($1, $2, $3)
           ON CONFLICT (owner_id) DO UPDATE
             SET manufacturer=$2, app_version=$3, updated_at=NOW()""",
        owner_id, manufacturer, app_version,
    )


async def apply_account_stage_event(pool: asyncpg.Pool, account_id: int, event: str) -> int:
    """Авто-переход CRM-стадии аккаунта на событии жизненного цикла.

    Идемпотентно и БЕЗ пред-чтения строки: из stage_flow берём (целевая стадия,
    список исходных стадий) и делаем ОДИН UPDATE ... WHERE stage IN (<sources>).
    Так авто-переход НЕ перетирает вручную выставленные стадии (frozen/reserve/…)
    — они просто не попадают в набор исходных. Возвращает число обновлённых строк.

    Не переживает отсутствие колонки stage (лаг миграции schema_v147) — тогда
    тихий no-op (как defensive-фолбэки proxy_fp)."""
    from services.stage_flow import stage_sources_for_event

    target, sources = stage_sources_for_event(event)
    if not target or not sources:
        return 0
    real = [s for s in sources if s]
    include_empty = "" in sources
    conds: list[str] = []
    args: list = [account_id, target]
    if real:
        args.append(real)
        conds.append(f"stage = ANY(${len(args)}::text[])")
    if include_empty:
        conds.append("(stage IS NULL OR stage = '')")
    where_stage = " OR ".join(conds) if conds else "FALSE"
    try:
        res = await pool.execute(
            f"UPDATE tg_accounts SET stage=$2 WHERE id=$1 AND ({where_stage})",
            *args,
        )
    except asyncpg.UndefinedColumnError:
        return 0
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0
