"""Меню Telegram-бота: список ботов показывает страницу, история рассылок —
все статусы без «❓»."""
from datetime import datetime

from bot import keyboards as kb


def _bots(n):
    return [{"bot_id": i, "username": f"b{i}", "first_name": "b", "audience_count": 0} for i in range(1, n + 1)]


def _texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_bot_list_shows_page_between_arrows():
    t = _texts(kb.bots_list(_bots(12), page=1))
    assert "📄 2/3" in t
    i = t.index("📄 2/3")
    assert t[i - 1] == "◀️" and t[i + 1] == "▶️"


def test_first_page_indicator_comes_before_next():
    t = _texts(kb.bots_list(_bots(7), page=0))
    i = t.index("📄 1/2")
    assert t[i + 1] == "▶️"


def test_single_page_has_no_indicator():
    assert not any(x.startswith("📄") for x in _texts(kb.bots_list(_bots(3), page=0)))


def test_broadcast_history_knows_failed_and_paused():
    rows = [{"id": i, "status": st, "created_at": datetime(2026, 11, 22, 12, 0),
             "sent_count": 1, "total_users": 2} for i, st in enumerate(["failed", "paused"], 1)]
    t = _texts(kb.broadcast_history(1, rows))
    assert t[0].startswith("⚠️") and t[1].startswith("⏸")
    assert not any("❓" in x for x in t)


def test_bot_list_has_a_way_back():
    t = _texts(kb.bots_list(_bots(2), page=0))
    assert "🏠 Главное меню" in t


def test_bot_count_is_declined():
    from bot.handlers.bots import _bots_word
    assert [_bots_word(n) for n in (1, 3, 5, 11, 21, 112)] == [
        "1 бот", "3 бота", "5 ботов", "11 ботов", "21 бот", "112 ботов"]


def test_network_menu_has_no_version_jargon():
    t = _texts(kb.network_ops_menu())
    assert "📢 Сетевая рассылка" in t
    assert not any("v2" in x for x in t)
    assert "📃 Краткое описание всем" in t
