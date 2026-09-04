"""Правка кампании: опечатка не должна стоить пересоздания.

Разрыв, который это закрывает: эндпоинта правки не было вовсе. Опечатка в
тексте означала удалить кампанию и собрать заново — заново выбрать аудиторию,
темп, лимит на аккаунт, медиа. Для инструмента, где текст пишут вручную, это
ежедневная потеря работы.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _body() -> str:
    start = _API.index("async def dm_campaign_update")
    return _API[start:start + 6000]


def test_update_endpoint_exists_and_is_routed():
    assert "async def dm_campaign_update" in _API
    assert 'add_patch("/api/miniapp/dm_campaign/{campaign_id}"' in _API


def test_update_is_owner_scoped():
    body = _body()
    assert "owner_id=$2" in body and "404" in body


def test_running_campaign_cannot_be_edited():
    """Смена текста на лету — часть людей получила бы одно, часть другое."""
    body = _body()
    assert 'row["status"] == "running"' in body and "409" in body


def test_audience_is_not_editable():
    """Смена аудитории на полпути превратила бы журнал отправок в кашу."""
    body = _body()
    for forbidden in ("target_type=", "target_id="):
        assert forbidden not in body, f"аудиторию править нельзя: {forbidden}"


def test_empty_values_are_rejected_not_silently_saved():
    body = _body()
    assert "Название не может быть пустым" in body
    assert "Текст не может быть пустым" in body


def test_text_goes_through_the_same_validation_as_create():
    body = _body()
    assert "validate_string" in body and "check_sql_suspicious" in body
    assert "max_len=4096" in body


def test_media_url_is_ssrf_checked_on_edit_too():
    """Правка — такой же путь ввода URL, как создание."""
    body = _body()
    assert "is_safe_public_url" in body


def test_pace_whitelist_enforced():
    body = _body()
    assert '("slow", "normal", "fast", "auto")' in body


def test_per_account_limit_clamped():
    body = _body()
    assert "max(1, min(200," in body


def test_partial_update_preserves_other_params():
    """params несут медиа, когорту и список получателей — их нельзя затирать
    только потому, что в запрос их не положили."""
    body = _body()
    assert 'row["params"]' in body
    assert "_p.pop(" in body and "_p[" in body


def test_nothing_to_change_is_an_explicit_error():
    body = _body()
    assert "Нечего изменять" in body


def test_quiet_hours_default_still_lives_in_the_engine():
    body = _body()
    assert '_p.pop("quiet_hours", None)' in body


# ── Интерфейс ─────────────────────────────────────────────────────────────────

def test_ui_has_edit_button_for_non_running():
    assert "editDm(" in _UI
    assert "c.status!=='running'?" in _UI


def test_ui_edit_sends_patch():
    assert "method:'PATCH'" in _UI


def test_ui_edit_locks_audience_and_restores_it_for_new_campaign():
    """Иначе после правки создание новой кампании осталось бы с заблокированным
    выбором аудитории."""
    assert "tt.disabled = true" in _UI
    assert "_tt.disabled=false" in _UI


def test_ui_edit_skips_bot_validation():
    assert "!CMP_EDIT_ID && needBot" in _UI


def test_ui_warns_when_part_of_audience_already_received_old_text():
    assert "им ушёл прежний текст" in _UI
