"""Ядро инвайтинга через общие папки: отбор чатов, лимиты, заголовок.

Метод: собрать свои каналы/чат в общую папку Telegram и раздать одну
chatlist-ссылку — по ней человек добавляет сразу всю связку. Здесь проверяется
доменная логика, которая должна отсекать негодный набор ДО обращения к
Telegram: пустой, слишком большой, из непригодных сущностей, с дублями, — а
также заголовок папки, который Telegram ограничивает 12 символами.
"""
from __future__ import annotations

from services import chatlist_folders as CF


# ── Заголовок ──────────────────────────────────────────────────────────────

def test_title_is_trimmed_to_telegram_limit():
    t = CF.clean_title("Очень длинное имя папки которое не влезет")
    assert len(t) <= CF.MAX_TITLE_LEN


def test_blank_title_gets_a_meaningful_fallback():
    assert CF.clean_title("   ") == "Подборка"
    assert CF.clean_title("") == "Подборка"


def test_short_title_is_kept_as_is():
    assert CF.clean_title("Крипта") == "Крипта"


# ── Отбор пригодных ─────────────────────────────────────────────────────────

def _chan(cid, **kw):
    return {"chat_id": cid, "title": f"ch{cid}", **kw}


def test_channels_are_eligible_bots_and_dms_are_not():
    assets = [
        _chan(1, type="channel"),
        _chan(2, type="supergroup"),
        _chan(3, type="bot"),
        _chan(4, type="private"),
    ]
    eligible, rejected = CF.select_eligible(assets)
    assert [a["chat_id"] for a in eligible] == [1, 2]
    assert {r["chat_id"] for r in rejected} == {3, 4}
    assert all(r["reason"] for r in rejected)


def test_managed_channels_without_a_type_are_treated_as_channels():
    """managed_channels не хранит тип, но это заведомо каналы — не отсеивать."""
    eligible, _ = CF.select_eligible([{"channel_id": 100, "title": "x"}])
    assert eligible and eligible[0]["chat_id"] == 100


def test_duplicates_are_dropped():
    eligible, rejected = CF.select_eligible([_chan(5), _chan(5), _chan(6)])
    assert [a["chat_id"] for a in eligible] == [5, 6]
    assert any(r.get("reason") == "дубль" for r in rejected)


def test_non_numeric_id_is_rejected_not_crashed():
    eligible, rejected = CF.select_eligible([{"chat_id": "abc", "type": "channel"}])
    assert not eligible and rejected[0]["reason"] == "нет числового chat_id"


# ── Валидация набора ────────────────────────────────────────────────────────

def test_empty_selection_is_refused_before_touching_telegram():
    ok, why, elig = CF.validate_selection([])
    assert ok is False and why and elig == []


def test_selection_of_only_bots_is_refused():
    ok, why, _ = CF.validate_selection([_chan(1, type="bot")])
    assert ok is False


def test_oversized_selection_is_refused_with_the_number():
    big = [_chan(i, type="channel") for i in range(CF.MAX_CHATS_PER_FOLDER + 5)]
    ok, why, elig = CF.validate_selection(big)
    assert ok is False
    assert str(len(elig)) in why


def test_a_normal_selection_passes():
    ok, why, elig = CF.validate_selection([_chan(1), _chan(2), _chan(3)])
    assert ok is True and why == "" and len(elig) == 3


# ── Запись и сводка ─────────────────────────────────────────────────────────

def test_record_carries_ids_count_and_optional_bundle_link():
    _ok, _why, elig = CF.validate_selection([_chan(1), _chan(2)])
    rec = CF.build_folder_record(42, "Моя папка", elig, instance_id=7)
    assert rec["owner_id"] == 42
    assert rec["chat_ids"] == [1, 2]
    assert rec["chat_count"] == 2
    assert rec["instance_id"] == 7


def test_record_title_is_already_trimmed():
    rec = CF.build_folder_record(1, "x" * 40, [_chan(1)])
    assert len(rec["title"]) <= CF.MAX_TITLE_LEN


def test_record_without_a_bundle_has_no_instance():
    rec = CF.build_folder_record(1, "t", [_chan(1)])
    assert rec["instance_id"] is None


def test_summary_states_when_there_is_no_link_yet():
    rec = CF.build_folder_record(1, "Крипта", [_chan(1), _chan(2)])
    s = CF.summarize(rec)
    assert "Крипта" in s and "2" in s and "ссылка ещё не создана" in s


def test_summary_reports_joins_when_present():
    rec = CF.build_folder_record(1, "Крипта", [_chan(1)])
    assert "добавили 9" in CF.summarize(rec, "https://t.me/addlist/x", join_count=9)
