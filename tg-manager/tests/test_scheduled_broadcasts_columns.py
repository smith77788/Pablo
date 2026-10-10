"""Регресс: КАЖДЫЙ writer scheduled_broadcasts использует реальные колонки схемы.

Баг: AI-инструмент «запланировать рассылку» (bot/utils/ai_tools.py) писал
`INSERT INTO scheduled_broadcasts(bot_id, owner_id, text, scheduled_at)` — но по
схеме (schema_v2) таких колонок НЕТ: правильные имена message_text / execute_at /
created_by. Значит INSERT падал ВСЕГДА → AI-планирование рассылки было мертво
(класс «несуществующая колонка = фича мертва»). Остальные писатели
(database/db.py, services/mini_app_api.py) уже используют верные колонки.

Гейт: находим все `INSERT INTO scheduled_broadcasts(...)` в коде и проверяем,
что каждая колонка в белом списке реальных колонок таблицы.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")

# Колонки таблицы scheduled_broadcasts: schema_v2.sql (базовые) +
# schema-миграция repeat_interval_min. Источник — CREATE TABLE + ADD COLUMN.
REAL_COLUMNS = {
    "id", "bot_id", "message_text", "execute_at", "status",
    "created_by", "created_at", "repeat_interval_min",
}

_SCAN_FILES = [
    "bot/utils/ai_tools.py",
    "database/db.py",
    "services/mini_app_api.py",
]

_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+scheduled_broadcasts\s*\(([^)]*)\)", re.IGNORECASE
)


def _iter_insert_columns():
    for rel in _SCAN_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for m in _INSERT_RE.finditer(src):
            cols = [c.strip().strip('"').lower() for c in m.group(1).split(",")]
            cols = [c for c in cols if re.match(r"^[a-z_][a-z0-9_]*$", c)]
            line = src[: m.start()].count("\n") + 1
            yield rel, line, cols


def test_scheduled_broadcasts_inserts_use_real_columns():
    offenders = []
    found_any = False
    for rel, line, cols in _iter_insert_columns():
        found_any = True
        bad = [c for c in cols if c not in REAL_COLUMNS]
        if bad:
            offenders.append(f"{rel}:{line} -> несуществующие колонки {bad}")
    assert found_any, "не найдено ни одного INSERT INTO scheduled_broadcasts — тест бесполезен"
    assert not offenders, "\n".join(offenders)


def test_ai_tool_schedule_uses_message_text_not_text():
    src = open(os.path.join(ROOT, "bot/utils/ai_tools.py"), encoding="utf-8").read()
    m = _INSERT_RE.search(src)
    assert m, "AI-инструмент должен вставлять в scheduled_broadcasts"
    cols = m.group(1).lower()
    assert "message_text" in cols and "execute_at" in cols and "created_by" in cols
    assert "owner_id" not in cols and "scheduled_at" not in cols, (
        "старые несуществующие колонки вернулись — INSERT снова будет падать"
    )


def test_ai_tool_bot_settings_no_dead_managed_bots_columns():
    """managed_bots не имеет колонок description/short_description и никто их не
    читает — источник правды описания бота Telegram (set_description). Раньше AI-
    инструмент писал `UPDATE managed_bots SET description=...` ПОСЛЕ успешного
    вызова API → падение обрывало инструмент. Запись убрана; тест стережёт возврат.
    """
    src = open(os.path.join(ROOT, "bot/utils/ai_tools.py"), encoding="utf-8").read()
    assert not re.search(
        r"UPDATE\s+managed_bots\s+SET\s+(description|short_description)\b", src, re.I
    ), "вернулась запись в несуществующую колонку managed_bots.(short_)description"
