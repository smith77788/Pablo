"""`scheduled_for` строкой ломал КАЖДУЮ отложенную операцию в продукте.

КАК НАШЛОСЬ. Прогон инвайта по НАСТОЯЩЕЙ базе (postgres 16, вся схема): попытка
поставить продолжение падала с

    invalid input for query argument $5:
    '2026-07-30T00:10:00+00:00' (expected a datetime.date or datetime.datetime
    instance, got 'str')

ПОЧЕМУ. `operation_bus.submit` объявлял `scheduled_for: Optional[str]`, а в
запросе стоит `$5::timestamptz`. Каст выглядит достаточной защитой — но asyncpg
выводит тип параметра ИЗ ЗАПРОСА: увидев timestamptz, он требует объект datetime
и на строке падает ещё до похода в Postgres. Парсить строку было НЕКОМУ.

ЧТО ЭТО ЛОМАЛО. Все продовые вызовы передают `.isoformat()`:
`mini_app_api.schedule_post`, `broadcaster` (две точки), `bot/handlers/mass_publish`,
автопродолжение инвайта. То есть отложенные посты, отложенные рассылки и массовая
публикация по расписанию НЕ СОЗДАВАЛИСЬ вовсе.

Отдельный урок про метод: на заглушке пула этого не видно вообще — фейковый
`fetchrow` принимает что угодно. Виден только на живом драйвере. Запись класса 15
в своде («спасает явный ::timestamptz») была неточной: с asyncpg каст не спасает,
нужен разбор на границе.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.operation_bus import _coerce_scheduled_for

ROOT = Path(__file__).resolve().parents[1]


def test_iso_string_becomes_aware_datetime():
    r = _coerce_scheduled_for("2026-07-30T00:10:00+00:00")
    assert isinstance(r, datetime), "asyncpg требует datetime, строку он отвергает"
    assert r.tzinfo is not None


def test_zulu_suffix_supported():
    r = _coerce_scheduled_for("2026-07-30T00:10:00Z")
    assert r == datetime(2026, 7, 30, 0, 10, tzinfo=timezone.utc)


def test_naive_string_is_treated_as_utc():
    """`scheduled_for` сравнивается с now() в timestamptz-колонке. Наивное время
    без пояса — молчаливый сдвиг на пояс сервера (свод, класс 10)."""
    assert _coerce_scheduled_for("2026-07-30T00:10:00").tzinfo == timezone.utc


def test_naive_datetime_is_treated_as_utc():
    assert _coerce_scheduled_for(datetime(2026, 7, 30, 0, 10)).tzinfo == timezone.utc


def test_aware_datetime_passes_through():
    d = datetime(2026, 7, 30, 0, 10, tzinfo=timezone.utc)
    assert _coerce_scheduled_for(d) is d


def test_none_stays_none():
    assert _coerce_scheduled_for(None) is None


def test_garbage_fails_loudly():
    """Тихо проглотить мусор нельзя: операция «на потом» просто не создастся."""
    with pytest.raises(ValueError):
        _coerce_scheduled_for("завтра утром")


def test_submit_actually_coerces():
    src = (ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")
    m = re.search(r"async def submit\(.*?return op_id", src, re.DOTALL)
    assert m and "_coerce_scheduled_for(scheduled_for)" in m.group(0), (
        "разбор обязан стоять на пути реального INSERT, а не лежать рядом"
    )


def test_production_callers_pass_strings():
    """Предпосылка: если однажды все перейдут на datetime, этот тест напомнит
    перечитать необходимость разбора (но убирать его всё равно нельзя — контракт
    публичный)."""
    callers = []
    for rel in ("services/broadcaster.py", "services/mini_app_api.py",
                "bot/handlers/mass_publish.py", "services/op_worker.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        if re.search(r"scheduled_for=\w*(iso|_at|sched)\w*", src) or "isoformat()" in src:
            callers.append(rel)
    assert len(callers) >= 3, (
        f"ожидались продовые вызовы со строкой, найдено: {callers}"
    )
