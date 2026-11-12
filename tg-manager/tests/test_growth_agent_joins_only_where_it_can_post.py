"""Агент роста вступает только туда, где сможет опубликовать, и сперва — в
самые тематичные группы.

НАХОДКА ПЕРВАЯ. Поиск отсеивал вещательные каналы, scam и restricted, но не
смотрел права участника. Агент вступал в группу, ждал 3–8 минут и только
тогда узнавал, что писать нельзя: чат закрыт для участников
(`default_banned_rights.send_messages`), ссылки запрещены, а в промо ссылка,
или вступление идёт заявкой админу. Каждое такое вступление — пустой след
аккаунта («вступил и молчит»), потерянный слот из пяти групп запуска и
10–20 минут паузы впустую.

НАХОДКА ВТОРАЯ. Из найденных групп брались случайные пять
(`random.shuffle`): тематичные стояли в том же списке, а промо уходило в
случайно найденные нетематичные (глобальный поиск Telegram на «фитнес»
приносит и барахолку, где однажды продали гантели).

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать `**posting_rights(chat)` из результата поиска -> красный
     test_search_marks_closed_groups;
  2. убрать проверку `postable` из filter_groups -> красный
     test_closed_groups_never_reach_the_join;
  3. вернуть `random.shuffle` вместо rank_groups -> красный
     test_executor_ranks_instead_of_shuffling.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import random
from types import SimpleNamespace

from services import niche_searcher as ns

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _rights(**kw):
    base = dict(send_messages=False, send_plain=False, embed_links=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_posting_rights_read_from_the_search_result():
    assert ns.posting_rights(SimpleNamespace(default_banned_rights=None)) == {
        "send_blocked": False, "links_blocked": False, "join_request": False}
    assert ns.posting_rights(SimpleNamespace(
        default_banned_rights=_rights(send_messages=True)))["send_blocked"]
    assert ns.posting_rights(SimpleNamespace(
        default_banned_rights=_rights(send_plain=True)))["send_blocked"]
    assert ns.posting_rights(SimpleNamespace(
        default_banned_rights=_rights(embed_links=True)))["links_blocked"]
    assert ns.posting_rights(SimpleNamespace(
        default_banned_rights=None, join_request=True))["join_request"]


def test_search_marks_closed_groups(monkeypatch):
    """Права приходят прямо в объекте чата из поиска — лишнего запроса нет."""
    import sys
    import types as _pytypes

    class _Chan(SimpleNamespace):
        pass

    fake = _pytypes.ModuleType("telethon.tl.types")
    fake.Channel = _Chan
    fake.Chat = type("Chat", (), {})
    monkeypatch.setitem(sys.modules, "telethon.tl.types", fake)
    chats = [
        _Chan(id=1, title="Фитнес чат", username="fit_open", participants_count=500,
              broadcast=False, default_banned_rights=_rights()),
        _Chan(id=2, title="Фитнес новости", username="fit_closed", participants_count=500,
              broadcast=False, default_banned_rights=_rights(send_messages=True)),
    ]

    async def _client(req):
        return SimpleNamespace(chats=chats)

    res = asyncio.new_event_loop().run_until_complete(
        ns._search_one_keyword(_client, "фитнес", 10, 0))
    by_id = {g["id"]: g for g in res}
    assert by_id[1]["send_blocked"] is False
    assert by_id[2]["send_blocked"] is True
    assert by_id[1]["keyword"] == "фитнес"


def test_closed_groups_never_reach_the_join():
    groups = [
        {"id": 1, "title": "Открытая", "members": 100},
        {"id": 2, "title": "Закрытая", "members": 100, "send_blocked": True},
        {"id": 3, "title": "По заявке", "members": 100, "join_request": True},
        {"id": 4, "title": "Без ссылок", "members": 100, "links_blocked": True},
    ]
    with_link = ns.filter_groups(groups, promo_has_links=True)
    assert [g["id"] for g in with_link] == [1]
    without_link = ns.filter_groups(groups, promo_has_links=False)
    assert [g["id"] for g in without_link] == [1, 4]
    # Старые вызовы без флага ведут себя как раньше.
    assert len(ns.filter_groups(groups)) == 4


def test_links_in_promo_are_detected():
    assert ns.text_has_links("Заходите https://example.com")
    assert ns.text_has_links("Канал t.me/mychannel")
    assert ns.text_has_links("Пишите @my_shop_bot")
    assert not ns.text_has_links("Просто текст без ссылок, скидка 20%")


def test_relevant_groups_go_first():
    groups = [
        {"id": 1, "title": "Барахолка Москва", "username": "baraholka", "members": 90000},
        {"id": 2, "title": "Фитнес Москва — тренировки", "username": "fit_msk", "members": 800},
        {"id": 3, "title": "Йога и фитнесу", "username": "yoga", "members": 3000},
    ]
    ranked = ns.rank_groups(groups, ["фитнес Москва", "тренировки"], geo="Москва",
                            rng=random.Random(1))
    assert ranked[0]["id"] == 2, ranked
    assert ranked[-1]["id"] == 1, (
        "огромная нетематичная группа обогнала тематичные — порядок снова "
        "решает размер, а не ниша")


def test_ranking_is_stable_for_equal_relevance_by_size():
    groups = [{"id": i, "title": "Фитнес", "members": m}
              for i, m in ((1, 10), (2, 100000))]
    ranked = ns.rank_groups(groups, ["фитнес"], rng=random.Random(0))
    assert ranked[0]["id"] == 2


def _growth_executor_source() -> str:
    src = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_exec_niche_growth_post":
            return ast.get_source_segment(src, node)
    raise AssertionError("исполнитель Агента роста не найден")


def test_executor_ranks_instead_of_shuffling():
    body = _growth_executor_source()
    assert "rank_groups(" in body
    assert "random.shuffle(groups)" not in body
    assert "promo_has_links=" in body
