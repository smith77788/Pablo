"""Виртуальный администратор ведёт канал на ЗАДАННОМ языке, не как придётся.

Жалоба владельца: ВА пишет то на русском, то на украинском. Язык выводился из
профиля канала (он мог быть смешанным/украинским), а не задавался явно. Теперь
язык — явный параметр канала (va_channel_admin.lang): '' = авто (как раньше),
'ru'/'uk'/'en' — жёсткая инструкция писать строго на нём.
"""
from __future__ import annotations

import inspect

from services import channel_admin as ca


# ── lang_directive ────────────────────────────────────────────────────────────

def test_lang_directive_is_hard_for_known_language():
    d = ca.lang_directive("uk")
    assert "украинском" in d and "СТРОГО" in d
    assert "Не смешивай" in d


def test_lang_directive_empty_for_auto():
    assert ca.lang_directive("") == ""
    assert ca.lang_directive(None) == ""
    assert ca.lang_directive("qq") == "", "неизвестный язык не даёт директиву"


# ── build_post_prompt впаивает язык в начало И конец ──────────────────────────

def test_prompt_forces_language_top_and_bottom():
    _sys, user = ca.build_post_prompt({"title": "Канал", "lang": "uk"}, pillar="новости")
    assert "украинском" in user
    # в начале (приоритет над профилем) и в конце (после примеров на др. языке)
    first = user.index("украинском")
    last = user.rindex("украинском")
    assert first < 200, "языковая директива должна стоять в начале промпта"
    assert last != first, "язык не повторён в конце — примеры могут увести модель"


def test_prompt_auto_has_no_hard_directive():
    _sys, user = ca.build_post_prompt({"title": "Канал", "lang": ""}, pillar="новости")
    assert "ЯЗЫК ПОСТА — СТРОГО" not in user


def test_russian_language_selected():
    _sys, user = ca.build_post_prompt({"title": "Канал", "lang": "ru"}, pillar="польза")
    assert "русском" in user and "ЯЗЫК ПОСТА — СТРОГО русском" in user


# ── validate_settings принимает/проверяет lang ────────────────────────────────

def test_validate_accepts_known_langs_and_auto():
    for lg in ("", "ru", "uk", "en"):
        out, errors = ca.validate_settings({"lang": lg})
        assert not errors, (lg, errors)
        assert out["lang"] == lg


def test_validate_rejects_unknown_lang():
    out, errors = ca.validate_settings({"lang": "zz"})
    assert errors and "lang" not in out


def test_validate_lang_is_optional_partial_update():
    # без lang в запросе — ключа нет в результате (частичное обновление)
    out, errors = ca.validate_settings({"topic": "кофе"})
    assert not errors and "lang" not in out


# ── lang виден на всех стыках БД/API ──────────────────────────────────────────

def test_lang_in_admin_cols_and_settings_public():
    assert "lang" in ca._ADMIN_COLS
    pub = ca.settings_public({"lang": "uk"})
    assert pub["lang"] == "uk"
    assert ca.settings_public(None)["lang"] == "", "умолчание — авто"


def test_save_settings_upsert_writes_lang():
    src = inspect.getsource(ca.save_settings)
    assert "lang=EXCLUDED.lang" in src, "upsert не сохраняет lang на конфликте"
    assert 'merged.get("lang")' in src, "значение lang не уходит в запрос"
