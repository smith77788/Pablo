"""Мини-апп не должен читать таблицу, в которую никто не пишет.

Такая таблица — самый тихий вид сломанного экрана: запрос выполняется, ошибки
нет, список всегда пуст, и человек читает пустоту как «всё хорошо». Так было с
«Здоровьем инфраструктуры»: экран читал `infrastructure_alerts`, а в неё за всю
историю продукта нет ни одного INSERT — её только создают (schema_v84), читают
и чистят по сроку. Настоящие находки копит `anomaly_detector` в
`anomaly_events`, каждые пять минут фоновым циклом. Мини-апп писал
«Инфраструктура работает нормально» ровно тогда, когда бот на том же материале
показывал критические аномалии.

Тот же класс однажды уже ловили поштучно — `test_ecosystem_dead_table.py` про
`ecosystem_channels`. Здесь проверка общая: любая новая мёртвая таблица в
читающем коде мини-аппа падает сразу, а не через полгода, когда кто-нибудь
заметит вечно пустой список.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"

#: Каталоги, которые не считаются продуктом: тесты и окружение.
_SKIP = ("/.git", "/node_modules", "/tests", "/.venv", "/__pycache__")


def _schema_tables() -> set[str]:
    """Таблицы, объявленные миграциями, — чтобы не принять за таблицу CTE."""
    names: set[str] = set()
    for path in ROOT.rglob("schema*.sql"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        names |= {
            m.lower()
            for m in re.findall(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", text, re.I
            )
        }
    return names


def _written_tables() -> set[str]:
    """Таблицы, в которые хоть где-то в продукте пишут."""
    written: set[str] = set()
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.sql")):
        if any(part in str(path) for part in _SKIP):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        written |= {m.lower() for m in re.findall(r"INSERT\s+INTO\s+([a-z_][a-z0-9_]*)", text, re.I)}
        written |= {m.lower() for m in re.findall(r"\bUPDATE\s+([a-z_][a-z0-9_]*)\s+SET", text, re.I)}
        written |= {m.lower() for m in re.findall(r"\bCOPY\s+([a-z_][a-z0-9_]*)", text, re.I)}
    return written


def _tables_read_by_miniapp() -> set[str]:
    src = API.read_text(encoding="utf-8")
    read = {m.lower() for m in re.findall(r"\bFROM\s+([a-z_][a-z0-9_]*)", src, re.I)}
    read |= {m.lower() for m in re.findall(r"\bJOIN\s+([a-z_][a-z0-9_]*)", src, re.I)}
    return read


def test_detector_sees_the_schema_and_the_writers():
    """Пустой список находок ничего не доказывает, пока измеритель не показал,
    что вообще видит таблицы и тех, кто в них пишет."""
    schema = _schema_tables()
    written = _written_tables()
    read = _tables_read_by_miniapp() & schema
    assert len(schema) > 200, f"таблиц в схеме найдено {len(schema)} — разбор миграций сломался"
    assert len(written) > 200, f"пишущих таблиц найдено {len(written)} — разбор кода сломался"
    assert len(read) > 50, f"мини-апп читает всего {len(read)} таблиц — разбор запросов сломался"
    # заведомо живая таблица обязана попасть в обе стороны
    assert "operation_queue" in schema and "operation_queue" in written


def test_miniapp_reads_no_table_that_nobody_writes():
    dead = sorted((_tables_read_by_miniapp() & _schema_tables()) - _written_tables())
    assert not dead, (
        "мини-апп читает таблицы, в которые в продукте нет ни одной записи — "
        "экран будет вечно пустым и прочитается как «всё хорошо»:\n  "
        + "\n  ".join(dead)
    )


def test_infra_health_reads_live_anomalies():
    """Экран здоровья берёт оповещения из живого источника, а не из пустоты."""
    src = API.read_text(encoding="utf-8")
    start = src.index("async def infra_health_overview")
    body = src[start : src.index("async def ", start + 10)]
    assert "get_active_anomalies" in body, (
        "оповещения на экране здоровья больше не берутся из anomaly_events — "
        "список снова станет вечно пустым")
    # имя таблицы остаётся в комментарии — он объясняет, почему её не читают;
    # проверяем именно запрос, а не упоминание
    sql_only = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "infrastructure_alerts" not in sql_only, (
        "вернулось чтение мёртвой таблицы infrastructure_alerts")


def test_infra_health_screen_shows_why():
    """У аномалии есть текст причины — экран обязан его показывать, иначе
    человек видит заголовок без объяснения, что именно сломалось."""
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function openInfraHealth")
    body = html[start : start + 2500]
    assert "a.description" in body, "экран здоровья не показывает причину аномалии"
    assert "a.severity" in body and "a.first_seen_at" in body
