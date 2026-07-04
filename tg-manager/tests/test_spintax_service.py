"""Тесты spintax-модуля: чистая логика без aiogram/openai и без сети LLM."""

from __future__ import annotations

import json

import pytest

from services import spintax_service as s


# ── parse_spin_response ──────────────────────────────────────────────────────


def test_parse_plain_json_array():
    assert s.parse_spin_response('["{A|B}", "{C|D}"]') == ["{A|B}", "{C|D}"]


def test_parse_strips_code_fence():
    text = '```json\n["{A|B}", "{C|D}"]\n```'
    assert s.parse_spin_response(text) == ["{A|B}", "{C|D}"]


def test_parse_fallback_to_spintax_lines():
    text = (
        "Вот варианты:\n"
        "1. {Привет|Здравствуйте} {мир|друг}\n"
        "2. {Хай|Привет} всем\n"
        "просто текст без спинтакса\n"
    )
    result = s.parse_spin_response(text)
    assert result == ["{Привет|Здравствуйте} {мир|друг}", "{Хай|Привет} всем"]


def test_parse_raises_on_garbage():
    with pytest.raises(s.SpintaxServiceError):
        s.parse_spin_response("совсем не спинтакс и не json")


# ── max_literal_word_run ─────────────────────────────────────────────────────


def test_literal_run_all_spun_is_zero():
    assert s.max_literal_word_run("{Привет|Хай} {мир|друг}") == 0


def test_literal_run_counts_words_between_groups():
    assert s.max_literal_word_run("{A|B} это три слова подряд {C|D}") == 4


def test_literal_run_ignores_nested_groups():
    assert s.max_literal_word_run("{сразу|в тот же {момент|час}}") == 0


# ── find_disallowed_tokens / quality_warnings ────────────────────────────────


def test_disallowed_tokens_clean():
    assert s.find_disallowed_tokens("{A|B} {C|D}") == []


def test_disallowed_tokens_flags_engine_syntax():
    tokens = s.find_disallowed_tokens("{Apple::10|Orange} [uuid] {!A|B}")
    assert "::" in tokens
    assert "[" in tokens
    assert "!" in tokens


def test_quality_warnings_clean_template():
    assert s.quality_warnings("{Привет|Хай} {мир|друг}") == []


def test_quality_warnings_flags_long_run():
    warnings = s.quality_warnings("{A|B} обычный текст совсем без всякого рандома тут")
    assert any("нерандомизированных" in w for w in warnings)


# ── engine-backed helpers ────────────────────────────────────────────────────


def test_is_valid_template():
    assert s.is_valid_template("{A|B}") is True
    assert s.is_valid_template("{A|B") is False


def test_keep_valid_templates_filters_broken():
    kept = s.keep_valid_templates(["{A|B}", "{oops", "{C|D}"])
    assert kept == ["{A|B}", "{C|D}"]


def test_expand_template_is_deterministic_under_seed():
    a = s.expand_template("{A|B|C|D}", seed=7)
    b = s.expand_template("{A|B|C|D}", seed=7)
    assert a == b


def test_expand_many_unique_count():
    variants = s.expand_many("{Купи|Возьми} {сейчас|сегодня|быстро}!", 4, unique=True)
    assert len(variants) == 4


# ── generate_spins (LLM инъектируется) ───────────────────────────────────────


async def _fake_complete_factory(response: str):
    async def _complete(system: str, user: str) -> str:
        _complete.calls.append((system, user))
        return response

    _complete.calls = []  # type: ignore[attr-defined]
    return _complete


@pytest.mark.asyncio
async def test_generate_spins_returns_valid_templates():
    complete = await _fake_complete_factory(
        json.dumps(["{Привет|Хай}, {как дела|как ты}?", "{Здравствуйте|Приветствую}!"])
    )
    result = await s.generate_spins("Привет, как дела?", complete=complete, count=2)
    assert result == ["{Привет|Хай}, {как дела|как ты}?", "{Здравствуйте|Приветствую}!"]
    system, user = complete.calls[0]
    assert "2" in system
    assert user == "Привет, как дела?"


@pytest.mark.asyncio
async def test_generate_spins_drops_invalid_and_keeps_valid():
    complete = await _fake_complete_factory(json.dumps(["{A|B}", "{broken", "{C|D}"]))
    result = await s.generate_spins("текст", complete=complete, count=3)
    assert result == ["{A|B}", "{C|D}"]


@pytest.mark.asyncio
async def test_generate_spins_empty_script_raises():
    complete = await _fake_complete_factory(json.dumps(["{A|B}"]))
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("   ", complete=complete, count=1)


@pytest.mark.asyncio
async def test_generate_spins_all_invalid_raises():
    complete = await _fake_complete_factory(json.dumps(["{broken", "also {broken"]))
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("текст", complete=complete, count=2)


@pytest.mark.asyncio
async def test_generate_spins_empty_response_raises():
    complete = await _fake_complete_factory("")
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("текст", complete=complete, count=2)
