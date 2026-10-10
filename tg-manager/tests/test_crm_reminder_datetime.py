"""Регресс: сохранение CRM-напоминания вообще работает (и в правильной таймзоне).

`uch_crm_reminder` передавал `remind_at` СТРОКОЙ в `upsert_crm`, а тот биндит
`next_reminder_at` (TIMESTAMPTZ) БЕЗ каста — asyncpg для timestamptz требует
`datetime`, а на строку кидает DataError. То есть кнопка «Напоминание» падала
500 при каждом нажатии: фича существовала в UI, но не работала никогда
(пользователь видел только «⚠️ Ошибка»).

Фикс — парсинг ISO на границе (хендлер): '...Z' → tz-aware UTC; naive-время
(старые клиенты) трактуется как UTC, чтобы не сдвигать; мусор → честный 400,
а не 500.
"""
from __future__ import annotations

import datetime as dtm
import re
from pathlib import Path

API = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"


def _handler_src() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def uch_crm_reminder.*?(?=\n    async def )", src, re.DOTALL)
    assert m, "uch_crm_reminder не найден"
    return m.group(0)


def test_reminder_parsed_to_datetime_not_string():
    h = _handler_src()
    assert "fromisoformat" in h, (
        "строка в TIMESTAMPTZ роняет asyncpg (DataError) — нужен разбор в datetime"
    )
    assert "'next_reminder_at': remind_dt" in h, "в upsert_crm должен уходить datetime"
    assert not re.search(r"'next_reminder_at':\s*data\.get\('remind_at'\)", h), (
        "сырая строка в timestamptz — фича падает 500"
    )


def test_naive_time_treated_as_utc():
    h = _handler_src()
    assert "tzinfo is None" in h and "timezone.utc" in h, (
        "naive-время обязано трактоваться как UTC, иначе сравнение с NOW() поедет"
    )


def test_bad_date_is_400_not_500():
    h = _handler_src()
    assert re.search(r"except \(TypeError, ValueError\):\s*\n\s*return _err\([^)]*400", h), (
        "мусорная дата — понятный 400, а не сырой 500"
    )


def test_parse_semantics_match_backend():
    """Контрольная проверка семантики разбора на реальных формах входа."""
    def parse(raw):
        d = dtm.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d.replace(tzinfo=dtm.timezone.utc) if d.tzinfo is None else d

    assert parse("2026-07-24T15:00:00.000Z").utcoffset() == dtm.timedelta(0)
    assert parse("2026-07-24T15:00").utcoffset() == dtm.timedelta(0)
    assert parse("2026-07-24T15:00:00+03:00").utcoffset() == dtm.timedelta(hours=3)
