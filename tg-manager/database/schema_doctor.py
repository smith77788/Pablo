"""Доктор схемы: самодиагностика и аддитивное самолечение БД при старте.

ЗАЧЕМ. Раннер миграций ведёт журнал ``schema_migrations`` и уже применённый
файл второй раз не проигрывает. Если миграция не доехала — коллизия basename
(так было с ``cf_relay_url`` в schema_v153: два агента заняли имя v153, раннер
счёл версию применённой и пропустил CF-файл), прерванный на середине деплой,
ручная правка прода — код начинает SELECT'ить колонку/таблицу, которых нет, и
падает ВЕСЬ путь, где объект упомянут («column a.cf_relay_url does not exist»).

Раньше это лечили РУЧНЫМ списком ``ADD COLUMN IF NOT EXISTS`` в ``main.py`` —
его надо пополнять руками на каждую новую колонку, и о забытой узнавали только
по падению в проде. Это ровно то, на что жалуется владелец: «система не умеет
сама создавать нужные таблицы и колонки, если их нет».

Доктор делает это ОБОБЩЁННО и без ручного списка. Источник ожиданий — сами
файлы ``schema_v*.sql`` (там объект и объявлен; их же применяет раннер). Доктор
выделяет ОЖИДАЕМЫЕ таблицы (``CREATE TABLE [IF NOT EXISTS] <name>``) и колонки
(``ALTER TABLE <name> ADD COLUMN <col>``), сверяет с фактической БД
(``information_schema``) и аддитивно до-создаёт только недостающее.

БЕЗОПАСНОСТЬ. Только аддитивный, идемпотентный DDL: ``CREATE TABLE IF NOT
EXISTS`` и ``ADD COLUMN IF NOT EXISTS`` (доктор дописывает ``IF NOT EXISTS``
сам, если в исходном стейтменте его не было, — поэтому тип колонки парсить не
нужно, он берётся из оригинала). Доктор НИЧЕГО не удаляет, не меняет типы и не
трогает данные. Расхождение, которое нельзя исправить аддитивно (другой тип,
лишняя колонка), он только фиксирует в отчёте, но не исправляет. Отчёт — в лог
(что проверено / создано / не удалось), как просил владелец.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── Разбор SQL файлов миграций ───────────────────────────────────────────────

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:"?public"?\.)?"?([a-zA-Z_][\w]*)"?',
    re.IGNORECASE,
)
_ALTER_TABLE_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r'(?:"?public"?\.)?"?([a-zA-Z_][\w]*)"?',
    re.IGNORECASE,
)
_ADD_COLUMN_RE = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][\w]*)\"?",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """Убрать ``-- …`` и ``/* … */`` — чтобы они не путали разбор и сплиттер."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def split_statements(sql: str) -> list[str]:
    """Разбить SQL на стейтменты по ``;`` на ВЕРХНЕМ уровне.

    Уважает одинарные/двойные кавычки и dollar-quoting (``$$…$$``,
    ``$tag$…$tag$``), чтобы ``;`` внутри строки или тела функции не рвал
    стейтмент. Скобки для сплита не важны: в DDL ``;`` внутри ``(...)`` не
    встречается (там запятые). Комментарии убираются заранее.
    """
    sql = _strip_sql_comments(sql)
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None          # "'" или '"'
    dollar: str | None = None         # активный доллар-тег, напр. "$$" или "$x$"
    while i < n:
        ch = sql[i]
        if dollar is not None:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote is not None:
            buf.append(ch)
            # Внутри строки '' / "" — экранированная кавычка, не конец.
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[a-zA-Z_]*\$", sql[i:])
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _make_idempotent(stmt: str) -> str:
    """Переписать стейтмент в идемпотентный вид (аддитивно-безопасный).

    - ``CREATE TABLE`` → ``CREATE TABLE IF NOT EXISTS``;
    - ``CREATE [UNIQUE] INDEX`` → ``… IF NOT EXISTS``;
    - каждый ``ADD COLUMN <col>`` → ``ADD COLUMN IF NOT EXISTS <col>``.

    Так heal не зависит от того, был ли ``IF NOT EXISTS`` в оригинале, и тип
    колонки берётся из самого стейтмента — парсить его не нужно.
    """
    s = stmt
    s = re.sub(r"(?i)\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\b)",
               "CREATE TABLE IF NOT EXISTS ", s)
    s = re.sub(r"(?i)\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS\b)",
               lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ", s)
    s = re.sub(r"(?i)\bADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS\b)",
               "ADD COLUMN IF NOT EXISTS ", s)
    return s


@dataclass
class ExpectedSchema:
    """Ожидаемое состояние схемы, собранное из файлов миграций."""

    # table -> идемпотентный CREATE TABLE стейтмент
    tables: dict[str, str] = field(default_factory=dict)
    # (table, column) -> идемпотентный ALTER … ADD COLUMN стейтмент
    columns: dict[tuple[str, str], str] = field(default_factory=dict)


def parse_expected_schema(sql_texts: list[str]) -> ExpectedSchema:
    """Собрать ожидаемые таблицы и ALTER-колонки из текстов миграций.

    Колонки берём ТОЛЬКО из ``ALTER TABLE … ADD COLUMN`` — именно поздние
    колонки чаще всего теряются при лаге/коллизии миграции, а тело
    ``CREATE TABLE`` целиком создаётся при до-создании таблицы. Это сознательно
    узкий, но надёжный скоуп: разбирать тело CREATE на колонки хрупко.
    """
    exp = ExpectedSchema()
    for text in sql_texts:
        for stmt in split_statements(text):
            head = stmt.lstrip()[:12].upper()
            if head.startswith("CREATE TABLE"):
                m = _CREATE_TABLE_RE.match(stmt.lstrip())
                if m:
                    exp.tables[m.group(1).lower()] = _make_idempotent(stmt)
            elif head.startswith("ALTER TABLE"):
                mt = _ALTER_TABLE_RE.match(stmt.lstrip())
                if not mt:
                    continue
                table = mt.group(1).lower()
                cols = _ADD_COLUMN_RE.findall(stmt)
                if not cols:
                    continue
                idem = _make_idempotent(stmt)
                for col in cols:
                    exp.columns[(table, col.lower())] = idem
    return exp


def _migration_texts(root: str) -> list[str]:
    """Тексты всех ``schema*.sql`` в порядке версии (как у раннера)."""
    paths = glob.glob(os.path.join(root, "schema*.sql")) + glob.glob(
        os.path.join(root, "database", "schema*.sql")
    )
    seen: set[str] = set()
    texts: list[str] = []

    def _ver(path: str) -> int:
        m = re.match(r"schema_v(\d+)", os.path.basename(path))
        return int(m.group(1)) if m else 0

    for path in sorted(paths, key=_ver):
        name = os.path.basename(path)
        if name in seen:
            continue
        seen.add(name)
        try:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        except OSError:
            log.warning("schema_doctor: не прочитан файл миграции %s", name)
    return texts


# ── Диагностика и лечение ────────────────────────────────────────────────────

@dataclass
class DoctorReport:
    checked_tables: int = 0
    checked_columns: int = 0
    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def healed_anything(self) -> bool:
        return bool(self.created_tables or self.added_columns)

    def as_log_line(self) -> str:
        parts = [
            f"проверено таблиц={self.checked_tables}",
            f"колонок={self.checked_columns}",
        ]
        if self.created_tables:
            parts.append("создано таблиц: " + ", ".join(sorted(self.created_tables)))
        if self.added_columns:
            parts.append("добавлено колонок: " + ", ".join(sorted(self.added_columns)))
        if self.errors:
            parts.append(f"ошибок: {len(self.errors)}")
        if not self.healed_anything and not self.errors:
            parts.append("расхождений нет")
        return "; ".join(parts)


async def _actual_tables(pool) -> set[str]:
    rows = await pool.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    return {r["table_name"].lower() for r in rows}


async def _actual_columns(pool) -> set[tuple[str, str]]:
    rows = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    )
    return {(r["table_name"].lower(), r["column_name"].lower()) for r in rows}


async def diagnose(pool, *, root: str | None = None,
                   expected: ExpectedSchema | None = None) -> tuple[list[str], list[tuple[str, str]], ExpectedSchema]:
    """Вернуть (недостающие таблицы, недостающие колонки, ожидаемая схема).

    Колонку считаем недостающей только если её таблица УЖЕ есть (иначе таблица
    создастся целиком со всеми колонками, и отдельный ALTER не нужен).
    """
    if expected is None:
        root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        expected = parse_expected_schema(_migration_texts(root))
    actual_tables = await _actual_tables(pool)
    actual_columns = await _actual_columns(pool)

    missing_tables = [t for t in expected.tables if t not in actual_tables]
    missing_columns = [
        (t, c) for (t, c) in expected.columns
        if t in actual_tables and (t, c) not in actual_columns
    ]
    return missing_tables, missing_columns, expected


async def heal(pool, *, root: str | None = None) -> DoctorReport:
    """Сверить ожидаемую схему с БД и аддитивно до-создать недостающее.

    Fail-soft: ошибка одного DDL не роняет старт — она уходит в отчёт, доктор
    продолжает. Самолечение не критично настолько, чтобы из-за него не поднять
    процесс; его задача — убрать тихие падения, а не добавить громкое.
    """
    report = DoctorReport()
    try:
        missing_tables, missing_columns, expected = await diagnose(pool, root=root)
    except Exception as e:  # БД недоступна/нет прав на information_schema
        report.errors.append(f"diagnose: {e}")
        log.warning("schema_doctor: диагностика не удалась: %s", e)
        return report

    report.checked_tables = len(expected.tables)
    report.checked_columns = len(expected.columns)

    for table in missing_tables:
        stmt = expected.tables[table]
        try:
            await pool.execute(stmt)
            report.created_tables.append(table)
            log.warning("schema_doctor: создана недостающая таблица %s", table)
        except Exception as e:
            report.errors.append(f"table {table}: {e}")
            log.warning("schema_doctor: не создать таблицу %s: %s", table, e)

    # Один ALTER может добавлять несколько колонок — выполняем каждый стейтмент
    # один раз (он идемпотентен: лишние колонки пропустятся по IF NOT EXISTS).
    done_stmts: set[str] = set()
    for (table, col) in missing_columns:
        stmt = expected.columns[(table, col)]
        if stmt not in done_stmts:
            done_stmts.add(stmt)
            try:
                await pool.execute(stmt)
            except Exception as e:
                report.errors.append(f"column {table}.{col}: {e}")
                log.warning("schema_doctor: не добавить %s.%s: %s", table, col, e)
                continue
        report.added_columns.append(f"{table}.{col}")
        log.warning("schema_doctor: добавлена недостающая колонка %s.%s", table, col)

    log.info("schema_doctor: %s", report.as_log_line())
    return report
