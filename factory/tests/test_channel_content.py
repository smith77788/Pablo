"""Tests for channel_content.py — Telegram channel post generator."""
from __future__ import annotations
import pytest
from factory.agents.channel_content import ChannelContentGenerator, POST_FORMATS, SEASONAL_THEMES


class TestChannelContentGenerator:
    def setup_method(self):
        self.gen = ChannelContentGenerator()

    def test_post_formats_list(self):
        assert isinstance(POST_FORMATS, list)
        assert len(POST_FORMATS) >= 4
        assert "model_spotlight" in POST_FORMATS
        assert "promotion" in POST_FORMATS

    def test_seasonal_themes_coverage(self):
        assert len(SEASONAL_THEMES) == 12
        for month in range(1, 13):
            assert month in SEASONAL_THEMES
            assert isinstance(SEASONAL_THEMES[month], str)


class TestModelSpotlightPost:
    def setup_method(self):
        self.gen = ChannelContentGenerator()
        self.model_data = {"name": "Анна", "age": 24, "height": 175, "category": "fashion", "city": "Москва", "order_count": 12}

    def test_returns_dict(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert isinstance(result, dict)

    def test_format_key(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert result["format"] == "model_spotlight"

    def test_text_contains_name(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert "Анна" in result["text"]

    def test_text_contains_hashtag(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert "#" in result["text"]

    def test_char_count_populated(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert result["char_count"] == len(result["text"])
        assert result["char_count"] > 50

    def test_model_name_in_result(self):
        result = self.gen.generate_model_spotlight_post(self.model_data)
        assert result["model_name"] == "Анна"

    def test_minimal_data_no_crash(self):
        result = self.gen.generate_model_spotlight_post({})
        assert isinstance(result, dict)
        assert "text" in result


class TestCaseStudyPost:
    def setup_method(self):
        self.gen = ChannelContentGenerator()
        self.order_data = {"event_type": "корпоратив", "city": "Москва", "model_count": 3, "duration_hours": 4}

    def test_returns_dict(self):
        result = self.gen.generate_case_study_post(self.order_data)
        assert isinstance(result, dict)

    def test_format_key(self):
        result = self.gen.generate_case_study_post(self.order_data)
        assert result["format"] == "case_study"

    def test_text_contains_event_type(self):
        result = self.gen.generate_case_study_post(self.order_data)
        assert "корпоратив" in result["text"].lower()

    def test_text_contains_city(self):
        result = self.gen.generate_case_study_post(self.order_data)
        assert "Москва" in result["text"]

    def test_text_is_html_formatted(self):
        result = self.gen.generate_case_study_post(self.order_data)
        assert "<b>" in result["text"]

    def test_empty_order_data_no_crash(self):
        result = self.gen.generate_case_study_post({})
        assert isinstance(result, dict)


class TestTipsPost:
    def setup_method(self):
        self.gen = ChannelContentGenerator()

    def test_returns_dict(self):
        result = self.gen.generate_tips_post()
        assert isinstance(result, dict)

    def test_format_key(self):
        result = self.gen.generate_tips_post()
        assert result["format"] == "tips"

    def test_text_has_numbered_tips(self):
        result = self.gen.generate_tips_post()
        assert "1." in result["text"]
        assert "2." in result["text"]

    def test_event_prep_topic(self):
        result = self.gen.generate_tips_post(topic="event_prep")
        assert result["topic"] == "event_prep"
        assert isinstance(result["text"], str)

    def test_unknown_topic_falls_back(self):
        result = self.gen.generate_tips_post(topic="unknown_topic_xyz")
        assert isinstance(result, dict)
        assert "text" in result


class TestPromotionPost:
    def setup_method(self):
        self.gen = ChannelContentGenerator()

    def test_returns_dict(self):
        result = self.gen.generate_promotion_post()
        assert isinstance(result, dict)

    def test_format_key(self):
        result = self.gen.generate_promotion_post()
        assert result["format"] == "promotion"

    def test_discount_in_text(self):
        result = self.gen.generate_promotion_post(discount_pct=20)
        assert "20" in result["text"]
        assert result["discount_pct"] == 20

    def test_promo_code_generated(self):
        result = self.gen.generate_promotion_post(discount_pct=15)
        assert result["promo_code"] == "CHANNEL15"
        assert "CHANNEL15" in result["text"]

    def test_valid_days_in_text(self):
        result = self.gen.generate_promotion_post(valid_days=3)
        assert result["valid_days"] == 3


class TestStatsPost:
    def setup_method(self):
        self.gen = ChannelContentGenerator()
        self.stats = {"total_orders": 45, "active_models": 12, "cities_served": 3, "avg_rating": 4.8}

    def test_returns_dict(self):
        result = self.gen.generate_stats_post(self.stats)
        assert isinstance(result, dict)

    def test_format_key(self):
        result = self.gen.generate_stats_post(self.stats)
        assert result["format"] == "stats"

    def test_order_count_in_text(self):
        result = self.gen.generate_stats_post(self.stats)
        assert "45" in result["text"]

    def test_rating_in_text(self):
        result = self.gen.generate_stats_post(self.stats)
        assert "4.8" in result["text"]

    def test_empty_stats_no_crash(self):
        result = self.gen.generate_stats_post({})
        assert isinstance(result, dict)


class TestContentCalendar:
    def setup_method(self):
        self.gen = ChannelContentGenerator()

    def test_returns_list(self):
        result = self.gen.get_content_calendar()
        assert isinstance(result, list)

    def test_4_weeks_returns_8_posts(self):
        result = self.gen.get_content_calendar(weeks=4)
        assert len(result) == 8

    def test_2_weeks_returns_4_posts(self):
        result = self.gen.get_content_calendar(weeks=2)
        assert len(result) == 4

    def test_each_item_has_required_keys(self):
        result = self.gen.get_content_calendar(weeks=1)
        for item in result:
            assert "week" in item
            assert "format" in item
            assert "recommended_time" in item
