"""Юнит-тесты чистой логики «Модератора чатов» (без БД/Telegram)."""
from __future__ import annotations

from services import chat_guard as cg


def test_merge_settings_fills_defaults():
    s = cg.merge_settings({"clean_join": False})
    assert s["clean_join"] is False           # переопределено
    assert s["clean_service"] is True         # дефолт
    assert s["max_warns"] == 3
    # неизвестные ключи игнорируются
    s2 = cg.merge_settings({"garbage": 1})
    assert "garbage" not in s2


def test_merge_settings_accepts_json_string():
    s = cg.merge_settings('{"antispam_links": true}')
    assert s["antispam_links"] is True
    # мусорная строка → чистые дефолты
    assert cg.merge_settings("not json")["clean_service"] is True


def test_should_delete_service_respects_master_switch():
    s = cg.merge_settings({"clean_service": False})
    # общий рубильник выключен → ничего не удаляем
    assert cg.should_delete_service("new_chat_members", s) is False
    assert cg.should_delete_service("pinned_message", s) is False


def test_should_delete_service_granular_toggles():
    s = cg.merge_settings({"clean_join": True, "clean_leave": False,
                           "clean_other": True})
    assert cg.should_delete_service("new_chat_members", s) is True     # join on
    assert cg.should_delete_service("left_chat_member", s) is False    # leave off
    assert cg.should_delete_service("pinned_message", s) is True       # other on
    assert cg.should_delete_service("new_chat_photo", s) is True
    # не-системный тип (обычный текст) — не удаляем никогда как «системный»
    assert cg.should_delete_service("text", s) is False


def test_service_type_sets_cover_common_notifications():
    # join/leave и типичные «прочие» реально попадают в наборы
    assert "new_chat_members" in cg.ALL_SERVICE_TYPES
    assert "left_chat_member" in cg.ALL_SERVICE_TYPES
    for t in ("pinned_message", "new_chat_title", "new_chat_photo",
              "video_chat_started", "message_auto_delete_timer_changed"):
        assert t in cg.ALL_SERVICE_TYPES
    # непересекающиеся группы
    assert not (cg.JOIN_TYPES & cg.LEAVE_TYPES)
    assert not (cg.JOIN_TYPES & cg.OTHER_SERVICE_TYPES)
