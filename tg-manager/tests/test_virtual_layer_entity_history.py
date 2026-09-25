"""История переходов сущности: «заходил и уходил» видно только по ней.

ЧТО БЫЛО. virtual_state_history писалась с самого первого дня слоя и не
читалась никем. Владелец видел текущее значение — «интерес» — и не мог
отличить человека, который пришёл к нему впервые, от человека, который третий
раз заходит и уходит. Дожимают их по-разному: первому нужен следующий шаг,
второму — другое предложение или честный отказ.

ЧТО ТЕПЕРЬ. Читается история, считается временной рисунок, и по горячей строке
и по боту на экране слоя можно провалиться внутрь.
"""
from __future__ import annotations

import datetime as dt

import pytest

from services import virtual_layer as vl


def _t(a, b):
    return {"from_value": a, "to_value": b}


def test_bouncing_is_distinguished_from_climbing():
    climbing = [_t(None, "curious"), _t("curious", "interested"),
                _t("interested", "ready")]
    bouncing = [_t(None, "curious"), _t("curious", "interested"),
                _t("interested", "curious"), _t("curious", "interested")]
    assert vl.temporal_pattern(climbing) == "climbing"
    assert vl.temporal_pattern(bouncing) == "bouncing", (
        "третий заход по кругу выглядит как первый подъём — решение будет "
        "принято неверно")


def test_only_falling_is_cooling():
    assert vl.temporal_pattern([_t("ready", "qualified"),
                                _t("qualified", "interested")]) == "cooling"
    assert vl.temporal_pattern([_t("interested", vl.LOST)]) == "cooling"


def test_no_transitions_is_steady():
    assert vl.temporal_pattern([]) == "steady"


def test_every_pattern_has_a_russian_label():
    for key in ("climbing", "bouncing", "cooling", "steady"):
        label = vl.PATTERN_LABEL[key]
        assert label and not any("a" <= c.lower() <= "z" for c in label), label


class _Pool:
    def __init__(self, rows, state=None, name=None):
        self.rows = rows
        self.state = state
        self.name = name

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchrow(self, sql, *args):
        return self.state

    async def fetchval(self, sql, *args):
        return self.name


def _row(a, b, when, reason="signal"):
    return {"from_value": a, "to_value": b, "reason": reason,
            "confidence": 0.7, "created_at": when}


async def test_history_is_returned_oldest_first_with_labels():
    now = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
    # База отдаёт новые первыми — функция обязана перевернуть.
    rows = [_row("curious", "interested", now),
            _row(None, "curious", now - dt.timedelta(hours=2))]
    out = await vl.entity_history(_Pool(rows), 1, vl.USER, "c-1")
    assert [t["to"] for t in out["transitions"]] == ["curious", "interested"]
    assert out["transitions"][1]["from_label"] == vl.VALUE_LABEL["curious"]
    assert out["pattern"] == "climbing"
    assert out["pattern_label"] == vl.PATTERN_LABEL["climbing"]


async def test_empty_history_never_raises():
    out = await vl.entity_history(_Pool([]), 1, vl.USER, "c-1")
    assert out["transitions"] == [] and out["pattern"] == "steady"


async def test_broken_database_gives_empty_history():
    class _Broken:
        async def fetch(self, *a):
            raise RuntimeError("база лежит")

    out = await vl.entity_history(_Broken(), 1, vl.USER, "c-1")
    assert out["transitions"] == []


# ── Поверхность: маршрут и экран ─────────────────────────────────────────────

def test_route_is_registered_and_scoped():
    src = open("services/mini_app_api.py", encoding="utf-8").read()
    assert ('add_get("/api/miniapp/vlayer/entity/{entity_type}/{entity_id}",'
            in src)
    i = src.index("async def vlayer_entity(")
    seg = src[i:src.index("\n    # ──", i)]
    assert "_get_uid(request)" in seg and "Unauthorized" in seg
    assert "entity_history(pool, uid," in seg, (
        "история читается без привязки к владельцу")
    assert "Неизвестный тип сущности" in seg, "тип сущности не проверяется"


def test_screen_lets_you_open_the_history():
    from tests.miniapp_source import miniapp_source
    ui = miniapp_source()
    assert "async function openVLayerEntity" in ui
    i = ui.index("async function openVLayerEntity")
    seg = ui[i:ui.index("\n}", i)]
    assert "/api/miniapp/vlayer/entity/" in seg
    assert "encodeURIComponent" in seg, "идентификатор уходит в адрес сырым"
    assert "openVLayer()" in seg, "из истории некуда вернуться"

    # И горячая строка, и бот ведут внутрь.
    j = ui.index("async function openVLayer")
    overview = ui[j:ui.index("\n}", j)]
    assert overview.count("openVLayerEntity(") >= 2, (
        "история открывается не отовсюду, откуда она нужна")


# ── Связывание параметров: заглушка пула этого не видит ──────────────────────

_DSN = __import__("os").getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not _DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
async def test_non_uuid_entity_id_does_not_break_the_binding():
    """id контакта — uuid, а в адресе бывает что угодно.

    asyncpg падает на СВЯЗЫВАНИИ («invalid UUID»), не на запросе, и заглушка
    пула такую ошибку не воспроизводит в принципе. Поэтому сравнение приводится
    в самом запросе, и проверяется это на живой базе.
    """
    import asyncpg

    conn = await asyncpg.connect(_DSN)
    try:
        assert await vl._entity_name(conn, 1, vl.USER, "не-uuid") is None
        assert await vl._entity_name(conn, 1, vl.BOT, "не-число") is None
    finally:
        await conn.close()
