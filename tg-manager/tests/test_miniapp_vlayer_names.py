"""Список «кого дожимать первыми» состоял из сырых идентификаторов.

Слой считает, на какой ступени воронки каждый человек, и экран показывал самых
«горячих» строкой «Готов · #7b1f…-uuid». Кто это — непонятно, действовать по
такому списку нечем. Имя при этом лежит в контактах ровно по этому
идентификатору: entity_id для типа user — это unified_contacts.id, по которому
в приложении уже открывается карточка контакта. То же в ленте событий
поведения: «🔥 Намерение купить · #<uuid>».

Имена берутся ПАЧКОЙ: в списках до полусотни строк, а запрос на строку — ровно
тот N+1, которым сводка и так нагружает базу на каждом открытии.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from services import virtual_layer as VL

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"

UID = 809404
CID = "7b1f0c22-0000-4000-8000-00000000abcd"


class _Pool:
    def __init__(self, contacts=(), bots=()):
        self.contacts = list(contacts)
        self.bots = list(bots)
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        if "GROUP BY value" in q:
            return [{"value": "ready", "c": 1}]
        if "FROM unified_contacts" in q:
            return self.contacts
        if "FROM managed_bots" in q:
            return self.bots
        if "LEFT JOIN managed_bots" in q:
            return []
        if "FROM virtual_states" in q:
            return [{"entity_type": "user", "entity_id": CID, "value": "ready",
                     "confidence": 0.9, "updated_at": datetime.now(timezone.utc)}]
        return []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        return None

    async def fetchval(self, q, *a):
        self.queries.append(q)
        return None


def test_resolve_names_reads_contacts_by_text_id():
    pool = _Pool(contacts=[{"eid": CID, "name": "Иван Петров"}])
    got = asyncio.run(VL.resolve_names(
        pool, UID, [{"entity_type": "user", "entity_id": CID}]))
    assert got == {CID: "Иван Петров"}
    q = [x for x in pool.queries if "unified_contacts" in x][0]
    assert "id::text = ANY($2::text[])" in q, (
        "сравнение uuid с текстом без приведения уронит запрос на мусорном id")


def test_resolve_names_prefixes_bots_with_at():
    pool = _Pool(bots=[{"eid": "555", "name": "salesbot"}])
    got = asyncio.run(VL.resolve_names(
        pool, UID, [{"entity_type": "bot", "entity_id": "555"}]))
    assert got == {"555": "@salesbot"}


def test_resolve_names_asks_once_per_type():
    """Иначе это запрос на каждую из полусотни строк списка."""
    pool = _Pool(contacts=[{"eid": CID, "name": "Иван"}])
    items = [{"entity_type": "user", "entity_id": CID} for _ in range(20)]
    asyncio.run(VL.resolve_names(pool, UID, items))
    assert sum("unified_contacts" in q for q in pool.queries) == 1


def test_resolve_names_is_fail_open():
    class _Broken(_Pool):
        async def fetch(self, q, *a):
            raise RuntimeError("база недоступна")

    got = asyncio.run(VL.resolve_names(
        _Broken(), UID, [{"entity_type": "user", "entity_id": CID}]))
    assert got == {}, "сбой имён не должен ронять сводку"


def test_overview_hot_carries_a_human_name():
    pool = _Pool(contacts=[{"eid": CID, "name": "Иван Петров"}])
    ov = asyncio.run(VL.overview(pool, UID))
    assert ov["hot"], "горячих не оказалось — проверять нечего"
    assert ov["hot"][0].get("name") == "Иван Петров", (
        "строка осталась «#<uuid>» — по такому списку работать нельзя")


# ── Мини-апп ─────────────────────────────────────────────────────────────────

def _fn(name: str) -> str:
    src = INDEX.read_text("utf-8")
    for prefix in (f"\nasync function {name}(", f"\nfunction {name}("):
        i = src.find(prefix)
        if i >= 0:
            return src[i:src.index("\n}", i) + 2]
    raise AssertionError(f"функция {name} не найдена")


def test_hot_row_shows_a_name_not_an_id():
    body = _fn("openVLayer")
    assert "h.name" in body, "имя с сервера не используется"
    assert "#${esc(String(h.entity_id))}" not in body, (
        "в строке «дожать» снова сырой идентификатор")


def test_events_row_shows_who_and_opens_the_entity():
    body = _fn("openVLayer")
    assert "'#'+esc(String(e.entity_id))" not in body, (
        "лента событий снова показывает сырой идентификатор")
    assert body.count("openVLayerEntity(") >= 2, (
        "из ленты событий нельзя открыть того, о ком она")


def test_nameless_entity_still_reads_as_something():
    """Имя контакта могло не сохраниться — сырой uuid всё равно не показываем."""
    body = _fn("_vlWho")
    assert "Контакт" in body and "slice(0, 8)" in body


def test_entity_screen_opens_the_contact_card():
    body = _fn("openVLayerEntity")
    assert "openContactDetail(" in body, (
        "история видна, а действовать по ней нечем: карточка человека не открывается")
