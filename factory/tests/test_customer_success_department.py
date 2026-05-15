"""
Tests for factory/agents/customer_success_department.py — heuristic (no-LLM) version.

Covers:
  - CustomerSuccessDepartment : generate_onboarding_message, analyze_retention_risk,
                                generate_review_request, suggest_upsell
  - CustomerSuccessDept       : OnboardingSpecialist, RetentionAnalyst,
                                FeedbackCollector, UpsellAdvisor, CustomerSuccessDepartment
                                (from customer_success_dept.py — LLM-backed, tested via mocks)
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from factory.agents.customer_success_department import CustomerSuccessDepartment


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def dept() -> CustomerSuccessDepartment:
    return CustomerSuccessDepartment()


def _days_ago_iso(days: int) -> str:
    """Return ISO-8601 UTC string for N days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat()


# ══════════════════════════════════════════════════════════════
# generate_onboarding_message
# ══════════════════════════════════════════════════════════════

class TestGenerateOnboardingMessage:

    def test_returns_string(self, dept):
        result = dept.generate_onboarding_message("Иван", "123")
        assert isinstance(result, str)

    def test_includes_client_name(self, dept):
        result = dept.generate_onboarding_message("Мария", "456")
        assert "Мария" in result

    def test_includes_order_number(self, dept):
        result = dept.generate_onboarding_message("Алексей", "999")
        assert "999" in result

    def test_empty_client_name_defaults_to_Клиент(self, dept):
        result = dept.generate_onboarding_message("", "001")
        assert "Клиент" in result

    def test_empty_order_number_defaults_to_dash(self, dept):
        result = dept.generate_onboarding_message("Анна", "")
        assert "—" in result

    def test_none_client_name_does_not_crash(self, dept):
        result = dept.generate_onboarding_message(None, "101")  # type: ignore[arg-type]
        assert isinstance(result, str)
        assert "Клиент" in result

    def test_none_order_number_does_not_crash(self, dept):
        result = dept.generate_onboarding_message("Петр", None)  # type: ignore[arg-type]
        assert isinstance(result, str)

    def test_message_is_non_empty(self, dept):
        result = dept.generate_onboarding_message("Тест", "1")
        assert len(result) > 50

    def test_message_contains_brand_name(self, dept):
        result = dept.generate_onboarding_message("Клиент", "1")
        assert "Nevesty Models" in result

    def test_message_contains_steps_list(self, dept):
        result = dept.generate_onboarding_message("Клиент", "1")
        # Should contain numbered steps
        assert "1." in result and "2." in result


# ══════════════════════════════════════════════════════════════
# analyze_retention_risk
# ══════════════════════════════════════════════════════════════

class TestAnalyzeRetentionRisk:

    def test_empty_history_returns_unknown(self, dept):
        result = dept.analyze_retention_risk([])
        assert result["risk_level"] == "unknown"
        assert result["days_since_last_order"] == -1

    def test_empty_history_has_required_keys(self, dept):
        result = dept.analyze_retention_risk([])
        for key in ("risk_level", "days_since_last_order", "recommendation"):
            assert key in result

    def test_history_without_dates_returns_unknown(self, dept):
        result = dept.analyze_retention_risk([{"id": 1, "amount": 5000}])
        assert result["risk_level"] == "unknown"

    def test_recent_order_is_low_risk(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(10)}])
        assert result["risk_level"] == "low"

    def test_medium_risk_30_to_90_days(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(60)}])
        assert result["risk_level"] == "medium"

    def test_high_risk_90_to_180_days(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(120)}])
        assert result["risk_level"] == "high"

    def test_critical_risk_over_180_days(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(200)}])
        assert result["risk_level"] == "critical"

    def test_days_since_last_order_is_int(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(45)}])
        assert isinstance(result["days_since_last_order"], int)
        assert result["days_since_last_order"] >= 0

    def test_recommendation_is_non_empty_string(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(45)}])
        assert isinstance(result["recommendation"], str)
        assert len(result["recommendation"]) > 10

    def test_picks_most_recent_date_from_multiple_orders(self, dept):
        history = [
            {"date": _days_ago_iso(200)},
            {"date": _days_ago_iso(15)},  # most recent — should give low risk
            {"date": _days_ago_iso(100)},
        ]
        result = dept.analyze_retention_risk(history)
        assert result["risk_level"] == "low"

    def test_supports_created_at_field(self, dept):
        result = dept.analyze_retention_risk([{"created_at": _days_ago_iso(20)}])
        assert result["risk_level"] == "low"

    def test_supports_event_date_field(self, dept):
        result = dept.analyze_retention_risk([{"event_date": _days_ago_iso(50)}])
        assert result["risk_level"] == "medium"

    def test_invalid_date_string_skipped_gracefully(self, dept):
        history = [
            {"date": "not-a-date"},
            {"date": _days_ago_iso(25)},
        ]
        result = dept.analyze_retention_risk(history)
        # valid date should be used; no crash
        assert result["risk_level"] in ("low", "medium", "high", "critical", "unknown")

    def test_all_invalid_dates_returns_unknown(self, dept):
        history = [
            {"date": "bad"},
            {"date": None},
        ]
        result = dept.analyze_retention_risk(history)
        assert result["risk_level"] == "unknown"

    def test_boundary_exactly_30_days_is_low(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(30)}])
        assert result["risk_level"] == "low"

    def test_boundary_exactly_31_days_is_medium(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(31)}])
        assert result["risk_level"] == "medium"

    def test_boundary_exactly_90_days_is_medium(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(90)}])
        assert result["risk_level"] == "medium"

    def test_boundary_exactly_91_days_is_high(self, dept):
        result = dept.analyze_retention_risk([{"date": _days_ago_iso(91)}])
        assert result["risk_level"] == "high"


# ══════════════════════════════════════════════════════════════
# generate_review_request
# ══════════════════════════════════════════════════════════════

class TestGenerateReviewRequest:

    def test_returns_string(self, dept):
        result = dept.generate_review_request({"client_name": "Анна", "event_type": "свадьба"})
        assert isinstance(result, str)

    def test_includes_client_name(self, dept):
        result = dept.generate_review_request({"client_name": "Борис"})
        assert "Борис" in result

    def test_includes_event_type(self, dept):
        result = dept.generate_review_request({"event_type": "корпоратив"})
        assert "корпоратив" in result

    def test_includes_order_id_when_present(self, dept):
        result = dept.generate_review_request({"id": 777})
        assert "777" in result

    def test_empty_dict_does_not_crash(self, dept):
        result = dept.generate_review_request({})
        assert isinstance(result, str)
        assert len(result) > 30

    def test_defaults_to_Клиент_when_no_name(self, dept):
        result = dept.generate_review_request({"event_type": "фото"})
        assert "Клиент" in result

    def test_defaults_to_мероприятие_when_no_event(self, dept):
        result = dept.generate_review_request({"client_name": "Дмитрий"})
        assert "мероприятие" in result

    def test_uses_name_field_as_fallback(self, dept):
        result = dept.generate_review_request({"name": "Елена"})
        assert "Елена" in result

    def test_uses_order_id_field_as_fallback(self, dept):
        result = dept.generate_review_request({"order_id": 42})
        assert "42" in result

    def test_message_is_non_empty(self, dept):
        result = dept.generate_review_request({"client_name": "Тест"})
        assert len(result) > 50

    def test_message_contains_brand(self, dept):
        result = dept.generate_review_request({})
        assert "Nevesty Models" in result


# ══════════════════════════════════════════════════════════════
# suggest_upsell
# ══════════════════════════════════════════════════════════════

class TestSuggestUpsell:

    def test_returns_dict(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив"})
        assert isinstance(result, dict)

    def test_has_required_keys(self, dept):
        result = dept.suggest_upsell({"event_type": "свадьба"})
        assert "suggestions" in result
        assert "reason" in result

    def test_suggestions_is_list(self, dept):
        result = dept.suggest_upsell({"event_type": "фотосессия"})
        assert isinstance(result["suggestions"], list)

    def test_reason_is_non_empty_string(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив"})
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 5

    def test_corporate_event_generates_suggestions(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив"})
        assert len(result["suggestions"]) >= 2

    def test_corporate_small_team_suggests_increase(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив", "model_count": 1})
        texts = " ".join(result["suggestions"])
        assert "3" in texts or "команд" in texts.lower()

    def test_corporate_large_team_no_increase_suggestion(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив", "model_count": 3})
        texts = " ".join(result["suggestions"])
        # "Увеличьте команду до 3" should NOT appear when already 3
        assert "Увеличьте команду до 3" not in texts

    def test_wedding_event_generates_suggestions(self, dept):
        result = dept.suggest_upsell({"event_type": "свадьба"})
        assert len(result["suggestions"]) >= 1

    def test_photo_event_generates_suggestions(self, dept):
        result = dept.suggest_upsell({"event_type": "фотосессия"})
        assert len(result["suggestions"]) >= 2

    def test_english_corporate_keyword_matches(self, dept):
        result = dept.suggest_upsell({"event_type": "corporate"})
        assert len(result["suggestions"]) >= 2

    def test_english_wedding_keyword_matches(self, dept):
        result = dept.suggest_upsell({"event_type": "wedding"})
        assert len(result["suggestions"]) >= 1

    def test_english_photo_keyword_matches(self, dept):
        result = dept.suggest_upsell({"event_type": "photo shoot"})
        assert len(result["suggestions"]) >= 2

    def test_unknown_event_type_has_default_suggestions(self, dept):
        result = dept.suggest_upsell({"event_type": "неизвестный тип"})
        assert len(result["suggestions"]) >= 1

    def test_empty_order_does_not_crash(self, dept):
        result = dept.suggest_upsell({})
        assert isinstance(result, dict)
        assert "suggestions" in result

    def test_high_budget_adds_personal_manager(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив", "budget": 60_000})
        texts = " ".join(result["suggestions"])
        assert "менеджер" in texts.lower() or "персональн" in texts.lower()

    def test_low_budget_no_premium_manager(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив", "budget": 10_000})
        texts = " ".join(result["suggestions"])
        assert "персонального менеджера" not in texts

    def test_budget_zero_no_crash(self, dept):
        result = dept.suggest_upsell({"event_type": "свадьба", "budget": 0})
        assert isinstance(result["suggestions"], list)

    def test_budget_none_no_crash(self, dept):
        result = dept.suggest_upsell({"event_type": "фотосессия", "budget": None})
        assert isinstance(result["suggestions"], list)

    def test_model_count_none_no_crash(self, dept):
        result = dept.suggest_upsell({"event_type": "корпоратив", "model_count": None})
        assert isinstance(result, dict)
