"""Строгие примитивы контроля набора SQL-миграций.

Боевой загрузчик ради обратной совместимости принимает старые имена и
дедуплицирует файлы по basename. Для CI это слишком мягкое поведение: ошибка в
имени или повтор версии может остаться незаметной. Этот модуль ничего не
применяет и не зависит от конфигурации приложения; он только проверяет
инварианты репозитория.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

_MIGRATION_RE = re.compile(
    r"^schema_v(?P<version>[1-9]\d*)(?:_[a-z0-9][a-z0-9_]*)?\.sql$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ChecksumEntry:
    """Одна строго разобранная строка манифеста контрольных сумм."""

    digest: str
    name: str
    line_number: int


def migration_version(name_or_path: str) -> int:
    """Вернуть объявленную версию или отклонить неоднозначное имя."""
    name = os.path.basename(name_or_path)
    if name == "schema.sql":
        return 0
    match = _MIGRATION_RE.fullmatch(name)
    if match is None:
        raise ValueError(f"неоднозначное имя миграции: {name}")
    return int(match.group("version"))


def canonical_migration_key(name_or_path: str) -> tuple[int, str]:
    """Полный детерминированный ключ, эквивалентный порядку загрузчика."""
    name = os.path.basename(name_or_path)
    return migration_version(name), name


def find_version_collisions(names: Iterable[str]) -> dict[int, tuple[str, ...]]:
    """Найти версии, занятые несколькими файлами независимо от суффикса."""
    grouped: dict[int, set[str]] = defaultdict(set)
    for item in names:
        name = os.path.basename(item)
        grouped[migration_version(name)].add(name)
    return {
        version: tuple(sorted(version_names))
        for version, version_names in sorted(grouped.items())
        if len(version_names) > 1
    }


def parse_checksum_entries(text: str) -> tuple[ChecksumEntry, ...]:
    """Строго разобрать манифест, не пропуская испорченные строки молча."""
    entries: list[ChecksumEntry] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(
                f"строка {line_number}: ожидаются sha256 и имя файла"
            )
        digest, name = parts
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"строка {line_number}: некорректный sha256")
        if name != os.path.basename(name):
            raise ValueError(
                f"строка {line_number}: вместо пути требуется имя файла"
            )
        migration_version(name)
        entries.append(ChecksumEntry(digest, name, line_number))
    return tuple(entries)
