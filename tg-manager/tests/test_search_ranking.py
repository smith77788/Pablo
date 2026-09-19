"""Реальная поисковая позиция считается СКВОЗНОЙ по всем типам выдачи.

Регресс на конкретный баг владельца: бот стоял 3-м (над ним два канала), а
система показывала 1-2, потому что позицию считали только среди пользователей/
ботов, игнорируя стоящие выше каналы. merge_ranked должен дать боту позицию 3.
"""
from __future__ import annotations

from services.search_ranking import merge_ranked
from services import channel_ranking as R


def _scenario():
    # Порядок выдачи Telegram: два канала, затем наш бот, затем ещё канал.
    order = [("channel", 10), ("channel", 20), ("user", 30), ("channel", 40)]
    users = {30: {"username": "MyBot", "is_bot": True, "first_name": "Бот"}}
    chats = {
        10: {"username": "chan_a", "title": "Канал A"},
        20: {"username": "chan_b", "title": "Канал B"},
        40: {"username": "chan_c", "title": "Канал C"},
    }
    return order, users, chats


def test_bot_position_is_global_not_users_only():
    ranked = merge_ranked(*_scenario())
    bot = next(r for r in ranked if r["is_bot"])
    assert bot["position"] == 3, "бот 3-й в общей выдаче (над ним 2 канала), не 1-й"


def test_channels_keep_real_positions():
    ranked = merge_ranked(*_scenario())
    by_id = {r["channel_id"]: r for r in ranked if r["channel_id"]}
    assert by_id[10]["position"] == 1
    assert by_id[20]["position"] == 2
    assert by_id[40]["position"] == 4


def test_find_position_uses_global_rank_for_channel():
    ranked = merge_ranked(*_scenario())
    # Наш канал chan_b стоит 2-м в ОБЩЕЙ выдаче — именно это и должно вернуться.
    assert R.find_position(ranked, channel_id=20) == 2
    assert R.find_position(ranked, username="@chan_c") == 4


def test_dedup_keeps_first_occurrence():
    order = [("channel", 10), ("channel", 10), ("user", 30)]
    users = {30: {"username": "b", "is_bot": True}}
    chats = {10: {"username": "c", "title": "C"}}
    ranked = merge_ranked(order, users, chats)
    assert [r["position"] for r in ranked] == [1, 2]
    assert len(ranked) == 2


def test_missing_entity_skipped():
    # пир есть в порядке, но сущности нет в users/chats — пропускаем, не падаем
    ranked = merge_ranked([("user", 99), ("channel", 10)], {}, {10: {"title": "C"}})
    assert len(ranked) == 1 and ranked[0]["channel_id"] == 10 and ranked[0]["position"] == 1
