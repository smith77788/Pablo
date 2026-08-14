"""Юнит-тесты чистой логики «Модератора чатов» (без БД/Telegram)."""
from __future__ import annotations

import time
from datetime import datetime

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


# ── Стоп-слова ────────────────────────────────────────────────────────────────

def test_find_stopword_word_boundaries():
    sw = ["спам", "купи слона"]
    assert cg.find_stopword("это СПАМ здесь", sw) == "спам"      # регистр не важен
    assert cg.find_stopword("нормальный текст", sw) is None
    # одиночное слово НЕ ловится внутри другого слова
    assert cg.find_stopword("приветствие всем", ["привет"]) is None
    assert cg.find_stopword("привет, друг", ["привет"]) == "привет"
    # фраза матчится как подстрока
    assert cg.find_stopword("срочно купи слона дёшево", sw) == "купи слона"
    assert cg.find_stopword("", sw) is None
    assert cg.find_stopword("текст", []) is None


# ── Ночной режим ──────────────────────────────────────────────────────────────

def test_is_night_simple_window():
    s = {"night_mode": True, "night_from": 1, "night_to": 6, "night_tz": 0}
    assert cg.is_night(s, datetime(2020, 1, 1, 3, 0)) is True
    assert cg.is_night(s, datetime(2020, 1, 1, 6, 0)) is False   # верхняя граница исключена
    assert cg.is_night(s, datetime(2020, 1, 1, 12, 0)) is False


def test_is_night_wraps_midnight_with_tz():
    s = {"night_mode": True, "night_from": 23, "night_to": 7, "night_tz": 3}
    # 21:00 UTC = 00:00 MSK → внутри окна 23→7
    assert cg.is_night(s, datetime(2020, 1, 1, 21, 0)) is True
    assert cg.is_night(s, datetime(2020, 1, 1, 1, 0)) is True    # 04:00 MSK
    assert cg.is_night(s, datetime(2020, 1, 1, 12, 0)) is False  # 15:00 MSK
    # выключено
    assert cg.is_night({"night_mode": False}, datetime(2020, 1, 1, 1, 0)) is False
    # from == to → трактуем как «выключено»
    assert cg.is_night({"night_mode": True, "night_from": 5, "night_to": 5},
                       datetime(2020, 1, 1, 5, 0)) is False


# ── Антифлуд ──────────────────────────────────────────────────────────────────

def test_flood_hit_triggers_at_threshold():
    cg.flood_reset(10, 20)
    now = time.time()
    triggered = False
    for i in range(6):                      # 6 сообщений — порог 7 не достигнут
        triggered = cg.flood_hit(10, 20, now + i * 0.1, 7, 10)
    assert triggered is False
    assert cg.flood_hit(10, 20, now + 0.7, 7, 10) is True   # 7-е → флуд
    cg.flood_reset(10, 20)
    assert cg.flood_hit(10, 20, now + 1, 7, 10) is False    # окно сброшено


def test_flood_window_slides():
    cg.flood_reset(11, 21)
    # два сообщения давно, окно узкое → старое выпадает, флуда нет
    assert cg.flood_hit(11, 21, 100.0, 2, 5) is False
    assert cg.flood_hit(11, 21, 106.0, 2, 5) is False   # 100.0 вне окна [101..106]
    cg.flood_reset(11, 21)
