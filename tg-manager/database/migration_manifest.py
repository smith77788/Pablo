"""Контрольные суммы файлов миграций — без зависимостей от конфигурации.

Модуль намеренно не импортирует ни `config`, ни `asyncpg`: им пользуются и
тест-храповик, и скрипт `deploy/scripts/lock_migrations.py`, который
запускают в голой среде без переменных окружения продукта.

Зачем контрольные суммы. Раннер миграций ведёт журнал `schema_migrations` и
уже применённый файл второй раз не проигрывает. Поэтому правка старого
`schema_vN.sql` на работающую базу не приедет никогда, а новое окружение
выполнит уже новый текст: схема прода расходится со схемой кода молча.
"""
from __future__ import annotations

import glob
import hashlib
import os


def migration_checksum(sql: str) -> str:
    """sha256 текста миграции в том виде, в каком его применяет раннер.

    Раннер читает файл и делает `.strip()` — сумма считается от той же
    строки, чтобы лишний перевод строки в конце файла не выглядел правкой
    миграции.
    """
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


def parse_checksum_manifest(text: str) -> dict[str, str]:
    """Разобрать `schema_checksums.txt`: {имя файла: sha256}.

    Строка — `<sha256>  <имя файла>`; пустые строки и строки с `#` в начале
    игнорируются, как в манифесте baseline.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        out[name] = digest
    return out


def collect_migration_checksums(root: str) -> dict[str, str]:
    """{имя файла: sha256} по всем файлам схемы репозитория.

    Ищет там же, где раннер: в корне `tg-manager` и в `database/`. Дедуп по
    базовому имени — как в `ordered_migration_files`, одно имя применяется
    один раз.
    """
    paths = glob.glob(os.path.join(root, "schema*.sql")) + glob.glob(
        os.path.join(root, "database", "schema*.sql")
    )
    out: dict[str, str] = {}
    for path in sorted(paths):
        name = os.path.basename(path)
        if name in out:
            continue
        with open(path, encoding="utf-8") as f:
            out[name] = migration_checksum(f.read())
    return out
