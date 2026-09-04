"""Персонализация ЛС: {name} обязана быть именем, а не словом «name».

Активный баг, который это чинит: spintax-движок видит `{name}` как группу из
ОДНОГО варианта и разворачивает её в голое слово. То есть очевидный синтаксис
персонализации молча портил текст — пользователь рассылал «Привет, name!»
тысячам людей. Хуже: в `{Привет|Хай}, {name}!` наивный фолбэк съедал и
spintax-группу.

Отсюда жёсткое требование к порядку: сначала персонализация, потом spintax.
"""
from __future__ import annotations

import pathlib

from services.dm_engine import (
    _DEFAULT_NAME_FALLBACK,
    expand_spintax,
    personalize,
    template_uses_personalization,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── Подстановка ───────────────────────────────────────────────────────────────

def test_name_uses_first_name():
    assert personalize("Привет, {name}!", {"first_name": "Иван"}) == "Привет, Иван!"


def test_name_falls_back_to_username():
    assert personalize("Привет, {name}!", {"username": "bob"}) == "Привет, @bob!"


def test_name_falls_back_to_neutral_word():
    """Пустая подстановка дала бы «Привет, !» — нужен нейтральный запасной вариант."""
    assert personalize("Привет, {name}!", {}) == f"Привет, {_DEFAULT_NAME_FALLBACK}!"
    assert personalize("Привет, {name}!", None) == f"Привет, {_DEFAULT_NAME_FALLBACK}!"


def test_custom_fallback_is_respected():
    assert personalize("Привет, {name}!", {}, fallback="коллега") == "Привет, коллега!"


def test_all_placeholders():
    t = {"first_name": "Иван", "last_name": "Петров", "username": "ivan"}
    assert personalize("{first_name} {last_name} {username}", t) == "Иван Петров @ivan"


def test_username_at_sign_not_doubled():
    assert personalize("{username}", {"username": "@ivan"}) == "@ivan"


def test_repeated_placeholder_substituted_everywhere():
    assert personalize("{name}, ещё раз {name}", {"first_name": "Ян"}) == "Ян, ещё раз Ян"


# ── Порядок с spintax (суть бага) ─────────────────────────────────────────────

def test_placeholder_survives_spintax_when_order_is_correct():
    out = expand_spintax(personalize("{Привет|Хай}, {name}!", {"first_name": "Иван"}))
    assert out in ("Привет, Иван!", "Хай, Иван!")
    assert "name" not in out


def test_documents_the_bug_if_order_were_reversed():
    """Если развернуть spintax ПЕРВЫМ, плейсхолдер превращается в слово «name».

    Тест фиксирует именно то поведение, ради которого введён порядок, — чтобы
    его нельзя было случайно поменять местами обратно.
    """
    broken = personalize(expand_spintax("Привет, {name}!"), {"first_name": "Иван"})
    assert broken == "Привет, name!"


def test_send_loop_personalizes_before_spintax():
    src = (_ROOT / "services" / "dm_engine.py").read_text(encoding="utf-8")
    assert "expand_spintax(personalize(" in src, (
        "в send-цикле персонализация обязана применяться ДО разворота spintax"
    )


# ── Устойчивость к враждебному имени ──────────────────────────────────────────

def test_hostile_display_name_cannot_inject_spintax():
    """Имя задаёт сам получатель — оно не должно становиться разметкой."""
    out = expand_spintax(personalize("{name}: привет", {"first_name": "Зло|{X|Y}"}))
    assert out.startswith("ЗлоXY"), out
    assert "|" not in out and "{" not in out


def test_long_name_is_truncated():
    out = personalize("{name}", {"first_name": "я" * 500})
    assert 0 < len(out) <= 64


def test_whitespace_only_name_falls_back():
    assert personalize("{name}", {"first_name": "   "}) == _DEFAULT_NAME_FALLBACK


def test_non_string_name_does_not_crash():
    assert personalize("{name}", {"first_name": 12345}) == "12345"


# ── Служебное ─────────────────────────────────────────────────────────────────

def test_template_without_placeholders_untouched():
    assert personalize("{А|Б} текст", {"first_name": "Иван"}) == "{А|Б} текст"
    assert personalize("", {"first_name": "И"}) == ""


def test_detector():
    assert template_uses_personalization("Привет, {name}!")
    assert not template_uses_personalization("{А|Б}")
    assert not template_uses_personalization("")


# ── Аудитория несёт имя ───────────────────────────────────────────────────────

def test_all_audience_queries_select_first_name():
    """Без имени в выборке персонализировать нечем."""
    src = (_ROOT / "services" / "dm_engine.py").read_text(encoding="utf-8")
    start = src.index("async def _get_targets")
    end = src.index("async def run_campaign")
    body = src[start:end]
    assert body.count("first_name") >= 10, (
        "каждый источник аудитории обязан отдавать first_name для персонализации"
    )


def test_ui_preview_matches_engine_order():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "{name\\}/g, 'Иван'" in ui or "\\{name\\}/g" in ui, (
        "предпросмотр обязан подставлять имя до spintax, иначе показывает «name»"
    )
