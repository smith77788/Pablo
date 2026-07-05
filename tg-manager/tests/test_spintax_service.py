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
    tokens = s.find_disallowed_tokens("{Apple::10|Orange} [uuid]")
    assert "::" in tokens
    assert "[" in tokens


def test_disallowed_tokens_allows_normal_punctuation():
    # ! ? # $ ~ — обычная пунктуация, не ошибка спинтакса
    assert s.find_disallowed_tokens("{Привет|Здравствуйте}! Как дела? #тема $ ~") == []


def test_quality_warnings_clean_template():
    assert s.quality_warnings("{Привет|Здравствуйте|Хай} {мир|друг|земля}") == []


def test_quality_warnings_flags_long_run():
    warnings = s.quality_warnings("{A|B} обычный текст совсем без всякого рандома тут")
    assert any("нерандомизированных" in w for w in warnings)


# ── first_option_render / preservation ───────────────────────────────────────


def test_first_option_render_takes_first_of_each_group():
    assert s.first_option_render("{Привет|Хай} {мир|друг}") == "Привет мир"


def test_first_option_render_nested():
    assert s.first_option_render("{сразу|в тот же {момент|час}}") == "сразу"
    assert s.first_option_render("{в тот же {момент|час}|сразу}") == "в тот же момент"


def test_first_option_render_reconstructs_original():
    orig = "Привет! Недавно подписался на ваш канал."
    tpl = "{Привет|Здравствуйте}! {Недавно|Не так давно} {подписался|подписалась} на {ваш канал|ваш паблик}."
    assert s.first_option_render(tpl) == orig


def test_preserves_original_true_for_faithful_spin():
    orig = "Привет! Случайно увидела, что ты сидишь тут."
    tpl = "{Привет|Хай}! {Случайно|Внезапно} {увидела|заметила}, что {ты|вы} {сидишь|тут} тут."
    assert s.preserves_original(tpl, orig) is True


def test_preserves_original_false_for_rewrite():
    orig = "Привет! Случайно увидела, что ты сидишь тут."
    rewrite = "{Здравствуйте|Приветствую}! {Наткнулась|Обнаружила} на то, что {вы|люди} зарегистрированы здесь непременно всенепременно навсегда."
    assert s.preserves_original(rewrite, orig) is False


# ── engine-backed helpers ────────────────────────────────────────────────────


def test_is_valid_template():
    assert s.is_valid_template("{Привет|Здравствуйте|Хай}") is True
    assert s.is_valid_template("{A|B}") is False  # <3 вариантов
    assert s.is_valid_template("{А|Б|В}") is True
    assert s.is_valid_template("{Привет|Здравствуйте}") is False  # <3
    assert s.is_valid_template("{Привет") is False


def test_keep_valid_templates_filters_broken():
    kept = s.keep_valid_templates(["{Привет|Здравствуйте|Хай}", "{oops", "{A|B|C}"])
    assert kept == ["{Привет|Здравствуйте|Хай}", "{A|B|C}"]


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
    # оба шаблона — честный спин исходного текста (скелет = оригинал), ≥3 варианта
    t1 = "{Привет|Здравствуйте|Хай}, {как дела|как ты|как поживаешь}?"
    t2 = "{Привет|Здравствуй|Хей}, {как дела|как жизнь|как сам}?"
    complete = await _fake_complete_factory(json.dumps([t1, t2]))
    result = await s.generate_spins("Привет, как дела?", complete=complete, count=2)
    assert result == [t1, t2]
    system, user = complete.calls[0]
    assert "2" in system
    assert user == "Привет, как дела?"


@pytest.mark.asyncio
async def test_generate_spins_drops_rewritten_template():
    # первый — честный спин, второй — переписан заново → отбрасывается фильтром
    orig = "Привет, недавно подписался на канал."
    faithful = "{Привет|Здравствуй|Хай}, {недавно|не так давно|на днях} {подписался|подписалась|подписалась только что} на {канал|паблик|сообщество}."
    rewrite = "{Здравствуйте|Приветствую|Доброго дня}! {Обнаружил|Выявил|Нашёл} интересный ресурс совершенно случайно недавно вечером."
    complete = await _fake_complete_factory(json.dumps([faithful, rewrite]))
    result = await s.generate_spins(orig, complete=complete, count=2)
    assert result == [faithful]


@pytest.mark.asyncio
async def test_generate_spins_drops_invalid_and_keeps_valid():
    complete = await _fake_complete_factory(json.dumps(["{Привет|Здравствуйте|Хай}", "{broken", "{мир|друг|земля}"]))
    result = await s.generate_spins("текст", complete=complete, count=3)
    assert result == ["{Привет|Здравствуйте|Хай}", "{мир|друг|земля}"]


def test_introduces_foreign_letters_detects_leak():
    # текст на русском, в шаблоне появилась латиница «práv»
    assert s.introduces_foreign_letters("{буквально|práv|совсем}", "буквально недавно") is True


def test_introduces_foreign_letters_allows_when_source_has_latin():
    # если латиница была в оригинале (бренд) — не считаем ошибкой
    assert s.introduces_foreign_letters("{Trading|Трейдинг}", "Trading курс") is False


def test_introduces_foreign_letters_clean_russian():
    assert s.introduces_foreign_letters("{Привет|Здравствуйте}", "Привет мир") is False


@pytest.mark.asyncio
async def test_generate_spins_drops_foreign_leak_templates():
    # первый шаблон с латиницей должен быть отброшен
    complete = await _fake_complete_factory(
        json.dumps(["{совсем|práv|буквально}", "{совсем|буквально|только что}"])
    )
    result = await s.generate_spins("совсем недавно", complete=complete, count=2)
    assert result == ["{совсем|буквально|только что}"]


@pytest.mark.asyncio
async def test_generate_spins_caps_to_requested_count():
    # модель вернула 9 валидных — наружу должно уйти ровно count
    complete = await _fake_complete_factory(
        json.dumps([f"{{A{i}|B{i}|C{i}}}" for i in range(9)])
    )
    result = await s.generate_spins("текст", complete=complete, count=5)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_generate_spins_empty_script_raises():
    complete = await _fake_complete_factory(json.dumps(["{Привет|Здравствуйте|Хай}"]))
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
