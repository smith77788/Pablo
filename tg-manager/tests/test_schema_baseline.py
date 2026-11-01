"""Baseline схемы: манифест и порядок файлов истории.

Чистая база проигрывает всю историю миграций подряд — сейчас это 194 файла.
Baseline сворачивает историю в снимок: файлы из манифеста помечаются
применёнными и не выполняются. Ошибка в манифесте молча пропустила бы
непроигранную миграцию, поэтому разбор манифеста и порядок файлов — под тестом.
"""
from __future__ import annotations

import importlib.util
import os

from database.db import parse_baseline_manifest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tool():
    path = os.path.join(ROOT, "deploy", "scripts", "make_schema_baseline.py")
    spec = importlib.util.spec_from_file_location("make_schema_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_ignores_comments_and_blanks():
    text = "# заголовок\nschema.sql\n\n  schema_v2.sql  # хвостовой комментарий\n#schema_v3.sql\n"
    assert parse_baseline_manifest(text) == {"schema.sql", "schema_v2.sql"}


def test_manifest_keeps_only_basenames():
    """В журнал миграций пишется базовое имя — путь в манифесте не должен
    порождать запись, которая ни с чем не совпадёт."""
    assert parse_baseline_manifest("database/schema_v7.sql\n") == {"schema_v7.sql"}


def test_manifest_of_empty_text_is_empty():
    assert parse_baseline_manifest("") == set()
    assert parse_baseline_manifest("# только комментарий\n") == set()


def test_tool_orders_files_like_runtime():
    """Порядок файлов у генератора обязан совпадать с порядком применения в
    рантайме: иначе --upto отрежет историю не там, где ожидает человек.

    Раньше тест сверял генератор сам с собой (оба склеивали все цифры имени),
    и schema_v190_geo_country_iso2.sql у обоих был «версией 1902»."""
    import glob

    from database.db import ordered_migration_files

    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert names, "не нашлось ни одного schema*.sql"
    assert names[0] == "schema.sql", "базовая схема должна идти первой"
    runtime = [os.path.basename(p) for p in ordered_migration_files(
        glob.glob(os.path.join(ROOT, "schema*.sql"))
        + glob.glob(os.path.join(ROOT, "database", "schema*.sql")))]
    assert names == runtime


def test_tool_never_includes_the_baseline_itself():
    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert not [n for n in names if n.startswith("schema_baseline")]


def test_tool_deduplicates_by_basename():
    """Файлы схемы лежат и в корне, и в database/ — дубль по имени в манифесте
    пометил бы применённым файл, который рантайм считает одним."""
    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert len(names) == len(set(names))


# ── Снимок, который лежит в репозитории ──────────────────────────────────────

def _committed():
    from database.db import baseline_paths

    sql_path, man_path = baseline_paths(ROOT)
    with open(sql_path, encoding="utf-8") as fh:
        sql = fh.read()
    with open(man_path, encoding="utf-8") as fh:
        manifest = parse_baseline_manifest(fh.read())
    return sql, manifest


def test_runtime_never_runs_the_snapshot_as_a_migration():
    """Регресс: снимок под именем `schema_baseline.sql` попадал под маску
    `schema*.sql`, и раннер исполнял его на КАЖДОЙ базе, включая продовую —
    поверх живой схемы, с пустым search_path из дампа."""
    from database.db import ordered_migration_files

    paths = [os.path.join(ROOT, n) for n in (
        "schema.sql", "schema_v2.sql", "schema_baseline.sql", "schema_baseline.manifest")]
    names = [os.path.basename(p) for p in ordered_migration_files(paths)]
    assert names == ["schema.sql", "schema_v2.sql"]


def test_snapshot_lives_outside_the_migration_mask():
    import glob

    from database.db import baseline_paths

    sql_path, man_path = baseline_paths(ROOT)
    masked = {os.path.abspath(p) for p in
              glob.glob(os.path.join(ROOT, "schema*.sql"))
              + glob.glob(os.path.join(ROOT, "database", "schema*.sql"))}
    assert os.path.abspath(sql_path) not in masked
    assert not os.path.basename(sql_path).startswith("schema")
    assert not os.path.exists(os.path.join(ROOT, "schema_baseline.sql")), (
        "снимок в корне под маской миграций — раннер исполнит его на проде")


def test_committed_snapshot_has_no_session_settings():
    """Снимок и хвост истории идут по одному соединению: `SET lock_timeout = 0`
    снял бы потолок ожидания блокировки, пустой search_path сломал бы каждый
    следующий файл, а `\\restrict` psql — синтаксическая ошибка для asyncpg."""
    sql, _manifest = _committed()
    bad = [ln for ln in sql.splitlines()
           if ln.startswith(("\\", "SET ", "SELECT pg_catalog.set_config("))]
    assert not bad, bad[:5]
    assert "schema_migrations" not in sql, (
        "журнал миграций заводит раннер до снимка — в снимке он ронял его целиком")


def test_sanitize_dump_strips_only_session_lines():
    tool = _load_tool()
    raw = ("\\restrict abc\nSET lock_timeout = 0;\n"
           "SELECT pg_catalog.set_config('search_path', '', false);\n"
           "CREATE FUNCTION public.f() RETURNS void LANGUAGE plpgsql AS $$\n"
           "BEGIN\n    SET LOCAL x = 1;\nEND $$;\n\\unrestrict abc\n")
    out = tool.sanitize_dump(raw)
    assert "restrict" not in out and "set_config" not in out
    assert "SET lock_timeout" not in out
    assert "    SET LOCAL x = 1;" in out, "тело функции трогать нельзя"


def test_manifest_is_a_prefix_of_history():
    """Снимок покрывает историю С НАЧАЛА и без дыр. Дыра означала бы файл,
    которого нет ни в снимке, ни в хвосте — его не выполнит никто."""
    tool = _load_tool()
    _sql, manifest = _committed()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    covered = [n for n in names if n in manifest]
    assert covered == names[: len(covered)], "в манифесте дыра посреди истории"
    stale = sorted(manifest - set(names))
    assert not stale, f"манифест называет файлы, которых нет: {stale[:5]}"


def test_history_tail_after_snapshot_stays_short():
    """Порог свёртки (docs/SCHEMA_CONSOLIDATION_PLAN.md, фаза 3): больше 40
    файлов после снимка — пора снимать новый. Иначе история снова растёт без
    предела, а снимок перестаёт что-либо сворачивать."""
    tool = _load_tool()
    _sql, manifest = _committed()
    tail = [os.path.basename(p) for p in tool.schema_files(ROOT)
            if os.path.basename(p) not in manifest]
    assert len(tail) <= 40, (
        f"после снимка накопилось {len(tail)} файлов истории — переснимите "
        "baseline: deploy/scripts/make_schema_baseline.py")
