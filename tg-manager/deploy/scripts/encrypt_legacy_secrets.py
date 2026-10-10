#!/usr/bin/env python3
"""
Разовый миграционный скрипт: шифрует ОСТАВШИЕСЯ plaintext-секреты в БД
(tg_accounts.session_str, user_proxies.proxy_url, booster_sessions.session_str
и booster_sessions.proxy) через уже работающий services/token_vault.py
(AES-256-GCM, тот же механизм, что шифрует bot-токены и SMM-панели).

════════════════════════════════════════════════════════════════════════════
 ВАЖНО — ПРОЧИТАТЬ ПЕРЕД ЗАПУСКОМ. НЕ ЗАПУСКАТЬ НА ПРОДЕ БЕЗ БЭКАПА.
════════════════════════════════════════════════════════════════════════════

Контекст: encrypt-on-write для этих трёх секретов УЖЕ активен в приложении
(db.add_tg_account, session_importer, bot/handlers/accounts.py re-auth,
bot/handlers/auto_registrar.py, mini_app_api.add_proxy, bot/handlers/
proxy_manager.py, database/db.py bb_add_session) — см. записи в
docs/AUDIT_LEDGER.md за 2026-07-07 и 2026-07-09. decrypt-on-read
(services/token_vault.decrypt_token) прозрачно читает СТАРЫЕ plaintext-
строки (passthrough по префиксу "ENC:"), поэтому приложение полностью
РАБОТАЕТ и без этой миграции — новые/перезаписанные строки уже шифруются
сами по себе ("ленивая" миграция при следующей записи).

Этот скрипт нужен ТОЛЬКО чтобы закрыть окно: если строка в БД годами не
перезаписывалась (аккаунт логинился один раз и с тех пор не трогался),
она по сей день лежит plaintext — при утечке бэкапа/SQL-инъекции/инсайдере
такие строки читаются как есть. Скрипт форсирует шифрование ВСЕХ ещё-
plaintext строк одним проходом, не дожидаясь органической перезаписи.

── Порядок запуска (production) ─────────────────────────────────────────────

  1. Сделать полный бэкап БД (pg_dump) — ОБЯЗАТЕЛЬНО. Без свежего бэкапа
     не запускать вообще, даже в dry-run (dry-run безопасен и без бэкапа,
     но привычку лучше не разбивать).
       pg_dump "$DATABASE_URL" -F c -f infragram_pre_encrypt_$(date +%Y%m%d).dump

  2. Прогнать БЕЗ --apply (dry-run — поведение по умолчанию). Скрипт
     посчитает и напечатает, сколько строк plaintext осталось в каждой
     таблице, НИЧЕГО не меняя в БД.
       python3 deploy/scripts/encrypt_legacy_secrets.py

  3. Сверить числа с ожиданиями (примерный размер базы аккаунтов/прокси).
     Если число неожиданно огромное или нулевое там, где не должно быть —
     остановиться и разобраться, прежде чем применять.

  4. Прогнать с --apply на STAGING или на восстановленной из бэкапа копии
     продовой БД — НЕ на проде первым делом.
       DATABASE_URL="$STAGING_DATABASE_URL" \\
       python3 deploy/scripts/encrypt_legacy_secrets.py --apply

  5. Проверить руками на staging: несколько аккаунтов логинятся, операции
     через прокси проходят, разница между старым и новым значением —
     только в том, что decrypt_token(new_value) == старое значение.

  6. Только после успешной проверки на staging — прогнать с --apply на
     проде, желательно в окно низкой нагрузки (скрипт батчит запросы и не
     держит длинные транзакции, но лучше не в пик активности пользователей).
       python3 deploy/scripts/encrypt_legacy_secrets.py --apply

  7. После прогона — прогнать регресс-тесты и проверить пару живых
     операций (login аккаунта, отправка сообщения через прокси-аккаунт):
       python3 -m pytest tests/test_session_encryption.py \\
           tests/test_proxy_encryption.py tests/test_booster_session_encryption.py

── План отката (если после миграции что-то сломалось) ───────────────────────

  - Само по себе шифрование НЕ меняет поведение чтения: decrypt_token
    распознаёт префикс "ENC:" и прозрачно снимает шифр для приложения —
    откат КОДА не требуется ни при каком сценарии.

  - Если проблема в самом прогоне (например, TOKEN_ENCRYPTION_KEY на
    момент прогона отличался от ключа, которым реально пользуется прод-
    процесс) — быстрый диагностический признак: аккаунты массово
    перестают логиниться сразу после миграции (decrypt на лету не может
    снять чужой ключ, decrypt_token ловит исключение и по дизайну
    возвращает СЫРОЕ значение как есть — то есть StringSession получит
    шифротекст вместо сессии и Telethon не подключится).
    → Диагностика: взять одну свежемигрированную строку, руками
      расшифровать services.token_vault.decrypt_token с ключом, которым
      реально запущен прод-процесс, и сверить с ожидаемым форматом сессии.

  - Восстановление: скрипт трогает ТОЛЬКО три таблицы (tg_accounts,
    user_proxies, booster_sessions) и только колонки session_str/
    session_fp/proxy_url/proxy_fp/proxy. Точечный откат — восстановить
    именно эти таблицы из бэкапа шага 1 (pg_restore -t tg_accounts -t
    user_proxies -t booster_sessions на отдельный инстанс, затем
    UPDATE ... FROM restored_copy), не трогая остальную БД. Полный
    restore всей базы — крайняя мера, только если точечный не сработал.

  - Скрипт идемпотентен и безопасен для повторного запуска: encrypt_token
    не шифрует повторно значение, уже начинающееся с "ENC:" (пропускает
    как есть), а выборка дополнительно фильтрует `NOT LIKE 'ENC:%'` —
    повторный прогон просто не найдёт строк для обработки.

── Использование ─────────────────────────────────────────────────────────────

  python3 deploy/scripts/encrypt_legacy_secrets.py                  # dry-run (по умолчанию, только счёт)
  python3 deploy/scripts/encrypt_legacy_secrets.py --apply          # реально шифрует
  python3 deploy/scripts/encrypt_legacy_secrets.py --apply --batch-size 200
  python3 deploy/scripts/encrypt_legacy_secrets.py --apply --only tg_accounts
  python3 deploy/scripts/encrypt_legacy_secrets.py --apply --only user_proxies,booster_sessions

DATABASE_URL берётся из окружения (тот же формат, что у основного приложения).

════════════════════════════════════════════════════════════════════════════
 ЭТОТ СКРИПТ НЕ БЫЛ И НЕ ДОЛЖЕН БЫТЬ ЗАПУЩЕН АВТОМАТИЧЕСКИ ПРОТИВ РЕАЛЬНОЙ/
 ПРОДАКШЕН БАЗЫ ДАННЫХ. Запуск — осознанное ручное действие человека,
 после бэкапа, сначала dry-run, потом staging, потом прод.
════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ── Табличные спецификации ───────────────────────────────────────────────────
# Каждая запись: (таблица, id-колонка, секретная колонка, fp-колонка|None,
#                 fp-функция|None, доп. секретная колонка|None (для booster proxy))
class TableSpec:
    def __init__(self, table: str, secret_col: str, fp_col: str | None = None):
        self.table = table
        self.secret_col = secret_col
        self.fp_col = fp_col


TABLE_SPECS: list[TableSpec] = [
    TableSpec("tg_accounts", "session_str", "session_fp"),
    TableSpec("user_proxies", "proxy_url", "proxy_fp"),
    TableSpec("booster_sessions", "session_str", None),
    TableSpec("booster_sessions", "proxy", None),
]


def _fp_func(spec: TableSpec):
    from services.token_vault import proxy_fingerprint, session_fingerprint

    if spec.secret_col == "proxy_url":
        return proxy_fingerprint
    return session_fingerprint  # session_str и booster .proxy — тот же приём (sha256 plaintext)


async def _count_plaintext(pool, spec: TableSpec) -> int:
    sql = (
        f"SELECT COUNT(*) FROM {spec.table} "
        f"WHERE {spec.secret_col} IS NOT NULL AND {spec.secret_col} <> '' "
        f"AND {spec.secret_col} NOT LIKE 'ENC:%'"
    )
    return await pool.fetchval(sql)


async def _migrate_table(pool, spec: TableSpec, batch_size: int, apply: bool) -> dict:
    """Батчами шифрует plaintext-строки. Возвращает статистику."""
    from services.token_vault import encrypt_token

    fp_fn = _fp_func(spec) if spec.fp_col else None
    stats = {"table": spec.table, "column": spec.secret_col, "seen": 0, "migrated": 0, "errors": 0}

    last_id = 0
    while True:
        rows = await pool.fetch(
            f"SELECT id, {spec.secret_col} AS secret FROM {spec.table} "
            f"WHERE id > $1 AND {spec.secret_col} IS NOT NULL AND {spec.secret_col} <> '' "
            f"AND {spec.secret_col} NOT LIKE 'ENC:%' "
            f"ORDER BY id LIMIT $2",
            last_id, batch_size,
        )
        if not rows:
            break

        for row in rows:
            stats["seen"] += 1
            last_id = row["id"]
            plaintext = row["secret"]
            try:
                encrypted = encrypt_token(plaintext)
                if not apply:
                    continue
                if spec.fp_col and fp_fn:
                    fp = fp_fn(plaintext)
                    await pool.execute(
                        f"UPDATE {spec.table} SET {spec.secret_col}=$1, {spec.fp_col}=$2 "
                        f"WHERE id=$3 AND {spec.secret_col}=$4",
                        encrypted, fp, row["id"], plaintext,
                    )
                else:
                    await pool.execute(
                        f"UPDATE {spec.table} SET {spec.secret_col}=$1 "
                        f"WHERE id=$2 AND {spec.secret_col}=$3",
                        encrypted, row["id"], plaintext,
                    )
                stats["migrated"] += 1
            except Exception as exc:  # noqa: BLE001 — одна плохая строка не должна рвать весь проход
                stats["errors"] += 1
                print(f"  ! {spec.table}.{spec.secret_col} id={row['id']}: {exc}", file=sys.stderr)

        if len(rows) < batch_size:
            break

    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Реально применить (по умолчанию — только dry-run счёт)")
    parser.add_argument("--batch-size", type=int, default=500, help="Размер батча (default: 500)")
    parser.add_argument(
        "--only", type=str, default="",
        help="Список таблиц через запятую (tg_accounts,user_proxies,booster_sessions). По умолчанию — все.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL не задан в окружении.", file=sys.stderr)
        return 1

    only = {t.strip() for t in args.only.split(",") if t.strip()}
    specs = [s for s in TABLE_SPECS if not only or s.table in only]

    import asyncpg

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        if not args.apply:
            print("=== DRY-RUN (ничего не меняется; для применения — флаг --apply) ===")
            total = 0
            for spec in specs:
                n = await _count_plaintext(pool, spec)
                total += n
                print(f"  {spec.table}.{spec.secret_col}: {n} plaintext-строк ожидают шифрования")
            print(f"Итого: {total} строк. Прогон с --apply зашифрует их через token_vault (AES-256-GCM).")
            return 0

        print(f"=== ПРИМЕНЕНИЕ (batch_size={args.batch_size}) ===")
        print("Убедитесь, что бэкап БД уже сделан (см. докстринг скрипта, шаг 1).")
        grand_total = {"seen": 0, "migrated": 0, "errors": 0}
        for spec in specs:
            stats = await _migrate_table(pool, spec, args.batch_size, apply=True)
            print(
                f"  {stats['table']}.{stats['column']}: seen={stats['seen']} "
                f"migrated={stats['migrated']} errors={stats['errors']}"
            )
            grand_total["seen"] += stats["seen"]
            grand_total["migrated"] += stats["migrated"]
            grand_total["errors"] += stats["errors"]
        print(
            f"Готово: обработано {grand_total['seen']}, зашифровано "
            f"{grand_total['migrated']}, ошибок {grand_total['errors']}."
        )
        if grand_total["errors"]:
            print(
                "ВНИМАНИЕ: были ошибки на отдельных строках — см. вывод выше. "
                "Остальные строки успешно смигрированы (батч не атомарен по всей таблице).",
                file=sys.stderr,
            )
            return 2
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
