"""
Tests for factory/agents/creative_department.py — standalone heuristic version.

Covers:
  - CreativeDepartment.generate_model_bio     : str output, field interpolation, defaults
  - CreativeDepartment.generate_social_caption : str output, known/unknown event types
  - CreativeDepartment.generate_promo_text    : str output, discount clamping, urgency text
  - CreativeDepartment.get_brand_voice_guidelines : dict structure, required keys
"""
from __future__ import annotations

import pytest

from factory.agents.creative_department import CreativeDepartment


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def dept() -> CreativeDepartment:
    return CreativeDepartment()


@pytest.fixture
def full_model_data() -> dict:
    return {
        "name": "Анна Иванова",
        "height": 175,
        "city": "Санкт-Петербург",
        "categories": ["подиум", "фэшн", "коммерческая"],
    }


@pytest.fixture
def minimal_model_data() -> dict:
    """Only required minimum — everything else should fall back to defaults."""
    return {}


@pytest.fixture
def model_data_string_categories() -> dict:
    return {
        "name": "Мария",
        "height": 168,
        "city": "Казань",
        "categories": "реклама",
    }


@pytest.fixture
def model_data_category_key() -> dict:
    """Uses 'category' (singular) instead of 'categories'."""
    return {
        "name": "Ольга",
        "city": "Новосибирск",
        "category": "реклама",
    }


# ══════════════════════════════════════════════════════════════
# TestGenerateModelBio
# ══════════════════════════════════════════════════════════════

class TestGenerateModelBio:
    """Tests for CreativeDepartment.generate_model_bio."""

    # -- return type & non-empty ---------------------------------

    def test_returns_string(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert isinstance(result, str)

    def test_result_is_non_empty(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert len(result) > 0

    # -- name interpolation --------------------------------------

    def test_name_appears_in_bio(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert "Анна Иванова" in result

    def test_missing_name_uses_default(self, dept, minimal_model_data):
        result = dept.generate_model_bio(minimal_model_data)
        assert "Модель" in result

    # -- city interpolation --------------------------------------

    def test_city_appears_in_bio(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert "Санкт-Петербург" in result

    def test_missing_city_uses_default(self, dept, minimal_model_data):
        result = dept.generate_model_bio(minimal_model_data)
        assert "Москва" in result

    # -- height interpolation ------------------------------------

    def test_height_appears_when_provided(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert "175" in result

    def test_missing_height_omitted_gracefully(self, dept, minimal_model_data):
        result = dept.generate_model_bio(minimal_model_data)
        # Should not crash; "рост" should be absent
        assert "рост" not in result

    def test_none_height_omitted_gracefully(self, dept):
        result = dept.generate_model_bio({"name": "Тест", "height": None})
        assert "рост" not in result

    # -- categories interpolation --------------------------------

    def test_list_categories_appear_in_bio(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert "подиум" in result
        assert "фэшн" in result

    def test_string_categories_appear_in_bio(self, dept, model_data_string_categories):
        result = dept.generate_model_bio(model_data_string_categories)
        assert "реклама" in result

    def test_category_singular_key_used_as_fallback(self, dept, model_data_category_key):
        result = dept.generate_model_bio(model_data_category_key)
        assert "реклама" in result

    def test_missing_categories_uses_default(self, dept, minimal_model_data):
        result = dept.generate_model_bio(minimal_model_data)
        assert "подиум" in result

    # -- brand mention -------------------------------------------

    def test_bio_contains_профессионал(self, dept, full_model_data):
        result = dept.generate_model_bio(full_model_data)
        assert "профессионал" in result.lower()

    # -- empty-string fields fall back to defaults ---------------

    def test_empty_name_falls_back_to_default(self, dept):
        result = dept.generate_model_bio({"name": ""})
        assert "Модель" in result

    def test_empty_city_falls_back_to_default(self, dept):
        result = dept.generate_model_bio({"city": ""})
        assert "Москва" in result


# ══════════════════════════════════════════════════════════════
# TestGenerateSocialCaption
# ══════════════════════════════════════════════════════════════

class TestGenerateSocialCaption:
    """Tests for CreativeDepartment.generate_social_caption."""

    # -- return type & non-empty ---------------------------------

    def test_returns_string(self, dept):
        result = dept.generate_social_caption("корпоратив", "Анна")
        assert isinstance(result, str)

    def test_result_is_non_empty(self, dept):
        result = dept.generate_social_caption("свадьба", "Мария")
        assert len(result) > 0

    # -- known event types ---------------------------------------

    def test_corporate_caption_contains_model_name(self, dept):
        result = dept.generate_social_caption("корпоратив", "Лена")
        assert "Лена" in result

    def test_wedding_caption_contains_model_name(self, dept):
        result = dept.generate_social_caption("свадьба", "Оля")
        assert "Оля" in result

    def test_photoshoot_caption_contains_model_name(self, dept):
        result = dept.generate_social_caption("фотосессия", "Катя")
        assert "Катя" in result

    def test_corporate_caption_contains_corporate_keyword(self, dept):
        result = dept.generate_social_caption("корпоратив", "Алина")
        assert "корпоратив" in result.lower()

    def test_wedding_caption_mentions_nevesty_models(self, dept):
        result = dept.generate_social_caption("свадьба", "Вера")
        assert "Nevesty Models" in result

    def test_photoshoot_caption_contains_photo_emoji_or_keyword(self, dept):
        result = dept.generate_social_caption("фотосессия", "Надя")
        assert "📸" in result or "фотосесси" in result.lower()

    # -- unknown event type (fallback) ---------------------------

    def test_unknown_event_returns_string(self, dept):
        result = dept.generate_social_caption("банкет", "Соня")
        assert isinstance(result, str)

    def test_unknown_event_contains_model_name(self, dept):
        result = dept.generate_social_caption("банкет", "Соня")
        assert "Соня" in result

    def test_unknown_event_contains_event_name(self, dept):
        result = dept.generate_social_caption("банкет", "Соня")
        assert "банкет" in result.lower()

    def test_unknown_event_contains_brand_hashtag(self, dept):
        result = dept.generate_social_caption("незнакомое событие", "Рита")
        assert "#nevesty" in result.lower() or "Nevesty" in result

    # -- case-insensitive matching for known events --------------

    def test_uppercase_event_type_matches_corporate(self, dept):
        result = dept.generate_social_caption("КОРПОРАТИВ", "Ира")
        assert "Ира" in result
        # Should use corporate template, not fallback
        assert "атмосфер" in result.lower() or "корпоратив" in result.lower()

    def test_mixed_case_event_type_matches_wedding(self, dept):
        result = dept.generate_social_caption("Свадьба", "Маша")
        # Should use wedding template
        assert "особенный" in result.lower() or "волшебн" in result.lower()

    # -- fallback when empty strings passed ----------------------

    def test_empty_event_type_does_not_crash(self, dept):
        result = dept.generate_social_caption("", "Нина")
        assert isinstance(result, str)

    def test_empty_model_name_does_not_crash(self, dept):
        result = dept.generate_social_caption("корпоратив", "")
        assert isinstance(result, str)


# ══════════════════════════════════════════════════════════════
# TestGeneratePromoText
# ══════════════════════════════════════════════════════════════

class TestGeneratePromoText:
    """Tests for CreativeDepartment.generate_promo_text."""

    # -- return type & non-empty ---------------------------------

    def test_returns_string(self, dept):
        result = dept.generate_promo_text(20, 5)
        assert isinstance(result, str)

    def test_result_is_non_empty(self, dept):
        result = dept.generate_promo_text(10, 3)
        assert len(result) > 0

    # -- discount appears in text --------------------------------

    def test_discount_value_in_text(self, dept):
        result = dept.generate_promo_text(30, 7)
        assert "30" in result

    def test_promo_code_contains_discount(self, dept):
        result = dept.generate_promo_text(25, 3)
        assert "NEVESTY25" in result

    def test_brand_name_in_promo_text(self, dept):
        result = dept.generate_promo_text(15, 2)
        assert "Nevesty Models" in result

    # -- urgency text --------------------------------------------

    def test_one_day_urgency_says_today(self, dept):
        result = dept.generate_promo_text(20, 1)
        assert "Только сегодня" in result

    def test_multi_day_validity_in_text(self, dept):
        result = dept.generate_promo_text(20, 7)
        assert "7" in result

    def test_2_days_uses_дня_form(self, dept):
        result = dept.generate_promo_text(20, 2)
        assert "дня" in result

    def test_5_or_more_days_uses_дней_form(self, dept):
        result = dept.generate_promo_text(20, 5)
        assert "дней" in result

    def test_10_days_uses_дней_form(self, dept):
        result = dept.generate_promo_text(15, 10)
        assert "дней" in result

    # -- discount clamping ---------------------------------------

    def test_discount_below_zero_clamped_to_zero(self, dept):
        result = dept.generate_promo_text(-10, 5)
        assert "0%" in result

    def test_discount_above_100_clamped_to_100(self, dept):
        result = dept.generate_promo_text(150, 5)
        assert "100%" in result

    def test_discount_at_boundary_0(self, dept):
        result = dept.generate_promo_text(0, 5)
        assert isinstance(result, str) and len(result) > 0

    def test_discount_at_boundary_100(self, dept):
        result = dept.generate_promo_text(100, 5)
        assert "100" in result

    # -- validity_days clamping ----------------------------------

    def test_validity_days_below_1_clamped_to_1(self, dept):
        result = dept.generate_promo_text(20, 0)
        assert "Только сегодня" in result

    def test_negative_validity_days_clamped(self, dept):
        result = dept.generate_promo_text(20, -5)
        assert isinstance(result, str) and len(result) > 0

    # -- contact CTA present ------------------------------------

    def test_telegram_cta_present(self, dept):
        result = dept.generate_promo_text(20, 3)
        assert "Telegram" in result


# ══════════════════════════════════════════════════════════════
# TestGetBrandVoiceGuidelines
# ══════════════════════════════════════════════════════════════

class TestGetBrandVoiceGuidelines:
    """Tests for CreativeDepartment.get_brand_voice_guidelines."""

    # -- return type ---------------------------------------------

    def test_returns_dict(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result, dict)

    # -- top-level required keys ---------------------------------

    def test_tone_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "tone" in result

    def test_style_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "style" in result

    def test_keywords_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "keywords" in result

    def test_avoid_words_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "avoid_words" in result

    def test_key_messages_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "key_messages" in result

    def test_channels_key_present(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "channels" in result

    # -- type checks on nested values ----------------------------

    def test_tone_is_non_empty_string(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["tone"], str) and len(result["tone"]) > 0

    def test_style_is_non_empty_string(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["style"], str) and len(result["style"]) > 0

    def test_keywords_is_list(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["keywords"], list)

    def test_keywords_list_non_empty(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert len(result["keywords"]) > 0

    def test_avoid_words_is_list(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["avoid_words"], list)

    def test_avoid_words_list_non_empty(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert len(result["avoid_words"]) > 0

    def test_key_messages_is_list(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["key_messages"], list)

    def test_key_messages_non_empty(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert len(result["key_messages"]) > 0

    def test_channels_is_dict(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert isinstance(result["channels"], dict)

    # -- channels sub-keys present -------------------------------

    def test_channels_has_telegram(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "telegram" in result["channels"]

    def test_channels_has_instagram(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "instagram" in result["channels"]

    def test_channels_has_website(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert "website" in result["channels"]

    # -- content sanity checks -----------------------------------

    def test_keywords_are_all_strings(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert all(isinstance(k, str) for k in result["keywords"])

    def test_key_messages_contain_brand_name(self, dept):
        result = dept.get_brand_voice_guidelines()
        messages_text = " ".join(result["key_messages"])
        assert "Nevesty Models" in messages_text

    def test_avoid_words_are_all_strings(self, dept):
        result = dept.get_brand_voice_guidelines()
        assert all(isinstance(w, str) for w in result["avoid_words"])

    def test_guidelines_deterministic(self, dept):
        """Calling twice should return identical results."""
        result1 = dept.get_brand_voice_guidelines()
        result2 = dept.get_brand_voice_guidelines()
        assert result1 == result2
