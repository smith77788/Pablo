"""Храповик целостности реестра SQL-миграций.

Старые коллизии версий уже могли быть применены в проде, поэтому переименовать
их задним числом нельзя. Мы фиксируем их точный состав и запрещаем появление
новых коллизий, неоднозначного порядка и файлов вне checksum-манифеста.
"""
from __future__ import annotations

import glob
import os
from collections import Counter
from itertools import pairwise
from pathlib import Path

from database.db import ordered_migration_files
from database.migration_guard import (
    canonical_migration_key,
    find_version_collisions,
    migration_version,
    parse_checksum_entries,
)
from database.migration_manifest import collect_migration_checksums

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "schema_checksums.txt"

# Эти коллизии исторические: журнал прода уже различает их по имени файла.
# Точное равенство не позволит незаметно добавить третий файл к старой версии.
LEGACY_VERSION_COLLISIONS = {
    146: ("schema_v146.sql", "schema_v146_ban_weather.sql"),
    185: ("schema_v185.sql", "schema_v185_wb_chat.sql"),
    193: ("schema_v193_audience_listener.sql", "schema_v193_channel_ownership.sql"),
    200: ("schema_v200_daughter_groups.sql", "schema_v200_discovered_bots.sql"),
    201: (
        "schema_v201_readiness_level_column.sql",
        "schema_v201_strike_account_outcomes.sql",
    ),
    206: ("schema_v206_bot_sales_persona_pro.sql", "schema_v206_showcase_layers.sql"),
    207: ("schema_v207_bot_sales_faq.sql", "schema_v207_warmup_interests.sql"),
    211: ("schema_v211_contacts_exclude.sql", "schema_v211_virtual_layer.sql"),
    212: ("schema_v212_bot_mesh.sql", "schema_v212_tg_cloud.sql"),
    213: ("schema_v213_bot_b2b.sql", "schema_v213_tg_cloud_replicas.sql"),
    219: ("schema_v219_scheduled_claim.sql", "schema_v219_vault_key_check.sql"),
    220: ("schema_v220_acct_wait_since.sql", "schema_v220_retention_indexes.sql"),
}


def _migration_paths() -> list[str]:
    """Повторить область поиска боевого загрузчика без собственной магии."""
    return glob.glob(str(ROOT / "schema*.sql")) + glob.glob(
        str(ROOT / "database" / "schema*.sql")
    )


def test_новые_коллизии_числовых_версий_запрещены():
    names = [os.path.basename(path) for path in _migration_paths()]
    actual = find_version_collisions(names)

    assert actual == LEGACY_VERSION_COLLISIONS, (
        "изменился набор коллизий версий миграций; новый файл обязан получить "
        "свободный номер, а исторические файлы нельзя переименовывать:\n"
        f"ожидалось: {LEGACY_VERSION_COLLISIONS}\nфактически: {actual}"
    )


def test_суффикс_не_скрывает_коллизию_версии():
    assert find_version_collisions(
        ["schema_v244.sql", "schema_v244_feature_2.sql"]
    ) == {244: ("schema_v244.sql", "schema_v244_feature_2.sql")}


def test_порядок_загрузчика_полный_и_однозначный():
    paths = _migration_paths()
    names = [os.path.basename(path) for path in paths]
    duplicate_names = sorted(
        name for name, count in Counter(names).items() if count > 1
    )
    assert not duplicate_names, (
        "загрузчик молча отбросит файлы с повторным basename: "
        + ", ".join(duplicate_names)
    )

    ordered = ordered_migration_files(paths)
    expected = sorted(paths, key=canonical_migration_key)
    assert ordered == expected, "фактический порядок загрузчика не совпал с каноном"
    assert len(ordered) == len(paths), "загрузчик молча потерял миграцию"

    keys = [canonical_migration_key(path) for path in ordered]
    assert all(left < right for left, right in pairwise(keys)), (
        "порядок миграций не является строгим и однозначным"
    )


def test_checksum_манифест_строгий_полный_и_без_дублей():
    entries = parse_checksum_entries(MANIFEST.read_text(encoding="utf-8"))
    manifest_names = [entry.name for entry in entries]
    duplicate_rows = sorted(
        name for name, count in Counter(manifest_names).items() if count > 1
    )
    assert not duplicate_rows, (
        "повторные строки checksum-манифеста маскируются обычным dict: "
        + ", ".join(duplicate_rows)
    )

    on_disk = collect_migration_checksums(str(ROOT))
    manifest = {entry.name: entry.digest for entry in entries}
    missing = sorted(set(on_disk) - set(manifest))
    stale = sorted(set(manifest) - set(on_disk))
    changed = sorted(
        name for name in set(on_disk) & set(manifest)
        if on_disk[name] != manifest[name]
    )
    assert not (missing or stale or changed), (
        "checksum-манифест не соответствует набору миграций: "
        f"не учтены={missing}, лишние={stale}, изменены={changed}. "
        "Новый файл зафиксируйте через "
        "`python deploy/scripts/lock_migrations.py`; исторический не изменяйте."
    )


def test_имена_миграций_разбираются_строго():
    for path in _migration_paths():
        migration_version(path)
