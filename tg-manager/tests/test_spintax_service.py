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


def _syn_json(*variant_sets: list) -> str:
    """Готовит JSON-ответ модели нового формата (наборы синонимов)."""
    return json.dumps({"variants": list(variant_sets)})


# ── assemble_template / _clean_synonyms / parse_synonym_response ──────────────


def test_assemble_template_preserves_original_and_wraps():
    script = "Привет, недавно подписался на канал."
    reps = [
        {"orig": "Привет", "syn": ["Здравствуй", "Хай"]},
        {"orig": "недавно", "syn": ["не так давно", "на днях"]},
        {"orig": "канал", "syn": ["паблик", "сообщество"]},
    ]
    tpl = s.assemble_template(script, reps)
    # текст сохранён 1-в-1: скелет == оригинал
    assert s.first_option_render(tpl) == script
    assert "{Привет|Здравствуй|Хай}" in tpl
    assert "{канал|паблик|сообщество}" in tpl


def test_assemble_template_skips_group_with_few_synonyms():
    script = "Привет мир."
    # у «мир» только 1 синоним → группа <3 → остаётся без скобок
    reps = [
        {"orig": "Привет", "syn": ["Здравствуй", "Хай"]},
        {"orig": "мир", "syn": ["земля"]},
    ]
    tpl = s.assemble_template(script, reps)
    assert "{Привет|Здравствуй|Хай}" in tpl
    assert "мир." in tpl and "{мир" not in tpl


def test_assemble_template_ignores_orig_not_in_text():
    script = "Привет мир."
    reps = [{"orig": "выдумка", "syn": ["ложь", "неправда"]}]
    # ничего не нашли для замены → шаблон == исходный текст
    assert s.assemble_template(script, reps) == script


def test_clean_synonyms_dedupe_foreign_and_case():
    # дубли и латиница отсеиваются, регистр подгоняется под orig
    out = s._clean_synonyms("Привет", ["привет", "Здравствуй", "hello", "хай", "хай"], "Привет мир")
    assert "hello" not in out
    assert "привет" not in [o.lower() for o in out if o == "привет"]  # не дубль orig
    assert "Здравствуй" in out
    assert "Хай" in out  # регистр подогнан под «Привет»


def test_parse_synonym_response_variants_and_flat():
    data = _syn_json([{"orig": "мир", "syn": ["земля"]}])
    assert s.parse_synonym_response(data, 1) == [[{"orig": "мир", "syn": ["земля"]}]]
    flat = json.dumps([{"orig": "мир", "syn": ["земля"]}])
    assert s.parse_synonym_response(flat, 1) == [[{"orig": "мир", "syn": ["земля"]}]]


def test_introduces_foreign_letters_detects_leak():
    assert s.introduces_foreign_letters("{буквально|práv|совсем}", "буквально недавно") is True


def test_introduces_foreign_letters_allows_when_source_has_latin():
    assert s.introduces_foreign_letters("{Trading|Трейдинг}", "Trading курс") is False


def test_introduces_foreign_letters_clean_russian():
    assert s.introduces_foreign_letters("{Привет|Здравствуйте}", "Привет мир") is False


# ── generate_spins (новый конвейер: синонимы → сборка) ───────────────────────


@pytest.mark.asyncio
async def test_generate_spins_assembles_faithful_template():
    script = "Привет, как дела?"
    complete = await _fake_complete_factory(
        _syn_json([
            {"orig": "Привет", "syn": ["Здравствуй", "Хай"]},
            {"orig": "как дела", "syn": ["как ты", "как жизнь"]},
        ])
    )
    result = await s.generate_spins(script, complete=complete, count=1)
    assert len(result) == 1
    # текст сохранён точь-в-точь
    assert s.first_option_render(result[0]) == script
    assert s.is_valid_template(result[0])
    system, user = complete.calls[0]
    assert user == script


@pytest.mark.asyncio
async def test_generate_spins_drops_foreign_synonyms():
    script = "совсем недавно"
    complete = await _fake_complete_factory(
        _syn_json([{"orig": "совсем", "syn": ["práv", "буквально", "только что"]}])
    )
    result = await s.generate_spins(script, complete=complete, count=1)
    # латиница «práv» отфильтрована, но группа всё равно собралась (≥2 чистых)
    assert "práv" not in result[0]
    assert s.first_option_render(result[0]) == script


@pytest.mark.asyncio
async def test_generate_spins_caps_to_requested_count():
    script = "Привет мир"
    sets = [
        [{"orig": "Привет", "syn": ["Здравствуй", "Хай"]}],
        [{"orig": "мир", "syn": ["земля", "планета"]}],
        [{"orig": "Привет", "syn": ["Хай", "Здорово"]}],
    ]
    complete = await _fake_complete_factory(_syn_json(*sets))
    result = await s.generate_spins(script, complete=complete, count=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_generate_spins_empty_script_raises():
    complete = await _fake_complete_factory(_syn_json([{"orig": "мир", "syn": ["земля"]}]))
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("   ", complete=complete, count=1)


@pytest.mark.asyncio
async def test_generate_spins_no_synonyms_raises():
    # модель не дала пригодных синонимов → нечего собирать
    script = "Привет мир"
    complete = await _fake_complete_factory(_syn_json([{"orig": "мир", "syn": ["земля"]}]))
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins(script, complete=complete, count=1)


@pytest.mark.asyncio
async def test_generate_spins_bad_json_raises():
    complete = await _fake_complete_factory("это не json")
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("текст", complete=complete, count=2)


@pytest.mark.asyncio
async def test_generate_spins_empty_response_raises():
    complete = await _fake_complete_factory("")
    with pytest.raises(s.SpintaxServiceError):
        await s.generate_spins("текст", complete=complete, count=2)
