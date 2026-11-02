"""Перебор по буквам добирает участников сверх потолка выдачи Telegram (D20).

ЧТО БЫЛО. Разбор шёл одним пустым запросом `ChannelParticipantsSearch("")`, а
Telegram отдаёт по нему стороннему аккаунту не больше ~10 тыс. участников —
у группы на 40 тыс. три четверти аудитории не собирались никогда, итог только
честно говорил «показал X из Y».

ЧТО СТАЛО. Когда `count` больше собранного, разбор идёт поиском по буквам
(латиница, цифры, кириллица): каждый поиск отдаёт свою порцию, повторы
отсекаются по id. У скрытого списка (только админы) буквы ничего не дают —
после нескольких пустых подряд перебор прекращается.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services import parser
from tests.test_parser_honest_outcome import _Entity, _Page, _U, _final, harness  # noqa: F401


@pytest.fixture(autouse=True)
def _real_requests(monkeypatch):
    """Telethon в тестах заглушен — запросу нужны настоящие поля q/offset/limit."""
    import telethon.tl.functions.channels as ch
    import telethon.tl.types as tt
    monkeypatch.setattr(ch, "GetParticipantsRequest",
                        lambda entity, flt, offset, limit, hash: SimpleNamespace(
                            filter=flt, offset=offset, limit=limit), raising=False)
    monkeypatch.setattr(tt, "ChannelParticipantsSearch",
                        lambda q: SimpleNamespace(q=q), raising=False)


class _SearchClient:
    """Группа из `total` человек; пустой запрос отдаёт только первых `cap`.
    Поиск по букве `q` отдаёт тех, чей номер даёт эту букву (по кругу)."""

    def __init__(self, total, cap, letters_work=True):
        self.total, self.cap, self.letters_work = total, cap, letters_work
        self.queries: list[str] = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_entity(self, ref):
        return _Entity()

    def _ids(self, q):
        if q == "":
            return list(range(self.cap))
        if not self.letters_work:
            return list(range(3))                    # одни и те же админы
        k = parser._SEARCH_ALPHABET.index(q)
        n = len(parser._SEARCH_ALPHABET)
        return [i for i in range(self.total) if i % n == k]

    async def __call__(self, request):
        q = request.filter.q
        if request.offset == 0:
            self.queries.append(q)
        ids = self._ids(q)[request.offset:request.offset + request.limit]
        return _Page([_U(i) for i in ids], self.total)


def test_letters_collect_beyond_the_ceiling(harness):
    c = _SearchClient(total=1500, cap=400)
    harness["client"] = c
    res = asyncio.run(parser.parse_members(None, 1, "@big", limit=5000))
    assert res["status"] == "done"
    assert res["total_found"] == 1500
    ids = [u["id"] for u in harness["saved"]]
    assert len(ids) == len(set(ids)) == 1500         # повторы не сохраняются
    assert not _final(harness)["error"]              # собрано всё — оговорки нет


def test_letters_stop_at_the_limit(harness):
    harness["client"] = _SearchClient(total=1500, cap=400)
    res = asyncio.run(parser.parse_members(None, 1, "@big", limit=600))
    assert res["total_found"] == 600


def test_hidden_list_stops_after_barren_letters(harness):
    c = _SearchClient(total=25000, cap=3, letters_work=False)
    harness["client"] = c
    res = asyncio.run(parser.parse_members(None, 1, "@hidden", limit=5000))
    assert res["total_found"] == 3
    letters = [q for q in c.queries if q]
    assert len(letters) == parser._SEARCH_BARREN_STOP
    assert "3 из 25000" in _final(harness)["error"]


def test_small_group_makes_no_letter_queries(harness):
    c = _SearchClient(total=120, cap=120)
    harness["client"] = c
    asyncio.run(parser.parse_members(None, 1, "@small"))
    assert c.queries == [""]
