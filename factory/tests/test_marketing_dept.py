"""
Tests for factory/agents/marketing_dept.py — БЛОК 5.2.

Covers:
  - ContentStrategist   : instantiation, department, method exists, API-graceful return
  - ViralEngineer       : instantiation, department, method exists, API-graceful return
  - SEOSpecialist       : instantiation, department, method exists, API-graceful return
  - AdCopywriter        : instantiation, department, method exists, API-graceful return
  - GrowthHacker        : instantiation, department, method exists, API-graceful return
  - MarketingDepartment : instantiation, agents wired, execute_task (content/viral/seo/growth)

All tests are API-graceful: think_json() returns {} or [] when no real API key —
methods must handle that gracefully (and do, via `or []` / `or {}`).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from factory.agents.marketing_dept import (
    ContentStrategist,
    ViralEngineer,
    SEOSpecialist,
    AdCopywriter,
    GrowthHacker,
    MarketingDepartment,
)


# ──────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def dept() -> MarketingDepartment:
    return MarketingDepartment()


@pytest.fixture
def sample_insights() -> dict:
    return {
        "top_channels": ["instagram", "tiktok"],
        "avg_engagement": 4.5,
        "target_audience": "организаторы мероприятий",
    }


@pytest.fixture
def sample_product() -> dict:
    return {
        "name": "Аренда моделей для корпоратива",
        "price_from": 15000,
        "usp": "Топ-модели за 4 часа",
    }


# ══════════════════════════════════════════════════════════════
# TestContentStrategist
# ══════════════════════════════════════════════════════════════

class TestContentStrategist:
    """ContentStrategist — unit tests."""

    def test_instantiation(self):
        agent = ContentStrategist()
        assert agent is not None

    def test_department_attribute(self):
        agent = ContentStrategist()
        assert agent.department == "marketing"

    def test_role_attribute(self):
        agent = ContentStrategist()
        assert agent.role == "content_strategist"

    def test_has_system_prompt(self):
        agent = ContentStrategist()
        assert isinstance(agent.system_prompt, str)
        assert len(agent.system_prompt) > 0

    def test_has_create_content_plan_method(self):
        agent = ContentStrategist()
        assert callable(getattr(agent, "create_content_plan", None))

    def test_create_content_plan_no_api_returns_list_or_dict(self):
        """Without a real API key think_json() returns {} → method returns []."""
        agent = ContentStrategist()
        result = agent.create_content_plan({})
        assert isinstance(result, (list, dict))

    def test_create_content_plan_empty_insights(self):
        agent = ContentStrategist()
        result = agent.create_content_plan({})
        # Should not raise; returns [] when no API
        assert result is not None

    def test_create_content_plan_with_insights(self, sample_insights):
        agent = ContentStrategist()
        result = agent.create_content_plan(sample_insights)
        assert isinstance(result, (list, dict))

    def test_create_content_plan_weeks_param(self):
        agent = ContentStrategist()
        result = agent.create_content_plan({}, weeks=4)
        assert isinstance(result, (list, dict))

    def test_create_content_plan_mocked_response(self):
        """With a mocked API response the method returns a list of dicts."""
        agent = ContentStrategist()
        mock_plan = [
            {"day": "Пн", "platform": "instagram", "topic": "Backstage", "format": "пост",
             "hook": "А вы знали?", "cta": "Оставить заявку"}
        ]
        with patch.object(agent, "think_json", return_value=mock_plan):
            result = agent.create_content_plan({"key": "val"})
        assert result == mock_plan
        assert isinstance(result[0], dict)
        assert "platform" in result[0]


# ══════════════════════════════════════════════════════════════
# TestViralEngineer
# ══════════════════════════════════════════════════════════════

class TestViralEngineer:
    """ViralEngineer — unit tests."""

    def test_instantiation(self):
        agent = ViralEngineer()
        assert agent is not None

    def test_department_attribute(self):
        agent = ViralEngineer()
        assert agent.department == "marketing"

    def test_role_attribute(self):
        agent = ViralEngineer()
        assert agent.role == "viral_engineer"

    def test_has_system_prompt(self):
        agent = ViralEngineer()
        assert len(agent.system_prompt) > 0

    def test_has_generate_video_scripts_method(self):
        agent = ViralEngineer()
        assert callable(getattr(agent, "generate_video_scripts", None))

    def test_generate_video_scripts_no_api_returns_list(self):
        agent = ViralEngineer()
        result = agent.generate_video_scripts()
        assert isinstance(result, (list, dict))

    def test_generate_video_scripts_with_count(self):
        agent = ViralEngineer()
        result = agent.generate_video_scripts(count=5)
        assert isinstance(result, (list, dict))

    def test_generate_video_scripts_with_trend(self):
        agent = ViralEngineer()
        result = agent.generate_video_scripts(count=2, trend="luxury models")
        assert isinstance(result, (list, dict))

    def test_generate_video_scripts_mocked_response(self):
        agent = ViralEngineer()
        mock_scripts = [
            {"title": "День из жизни модели", "hook": "Вы не поверите...",
             "script": "Сценарий тут", "hashtags": ["#models"], "music_style": "pop",
             "duration_sec": 30}
        ]
        with patch.object(agent, "think_json", return_value=mock_scripts):
            result = agent.generate_video_scripts(count=1)
        assert result == mock_scripts
        assert "hook" in result[0]


# ══════════════════════════════════════════════════════════════
# TestSEOSpecialist
# ══════════════════════════════════════════════════════════════

class TestSEOSpecialist:
    """SEOSpecialist — unit tests."""

    def test_instantiation(self):
        agent = SEOSpecialist()
        assert agent is not None

    def test_department_attribute(self):
        agent = SEOSpecialist()
        assert agent.department == "marketing"

    def test_role_attribute(self):
        agent = SEOSpecialist()
        assert agent.role == "seo_specialist"

    def test_has_system_prompt(self):
        agent = SEOSpecialist()
        assert len(agent.system_prompt) > 0

    def test_has_generate_seo_cluster_method(self):
        agent = SEOSpecialist()
        assert callable(getattr(agent, "generate_seo_cluster", None))

    def test_generate_seo_cluster_no_api_returns_dict(self):
        agent = SEOSpecialist()
        result = agent.generate_seo_cluster("аренда моделей")
        assert isinstance(result, (dict, list))

    def test_generate_seo_cluster_empty_keyword(self):
        agent = SEOSpecialist()
        result = agent.generate_seo_cluster("")
        assert result is not None

    def test_generate_seo_cluster_mocked_response(self):
        agent = SEOSpecialist()
        mock_cluster = {
            "main_keyword": "аренда моделей Москва",
            "volume": "3000/мес",
            "pillar_article": {"title": "Аренда моделей в Москве", "meta": "...", "h2s": []},
            "cluster_articles": [],
            "internal_links": [],
            "schema_type": "LocalBusiness"
        }
        with patch.object(agent, "think_json", return_value=mock_cluster):
            result = agent.generate_seo_cluster("аренда моделей Москва")
        assert result["main_keyword"] == "аренда моделей Москва"
        assert "pillar_article" in result


# ══════════════════════════════════════════════════════════════
# TestAdCopywriter
# ══════════════════════════════════════════════════════════════

class TestAdCopywriter:
    """AdCopywriter — unit tests."""

    def test_instantiation(self):
        agent = AdCopywriter()
        assert agent is not None

    def test_department_attribute(self):
        agent = AdCopywriter()
        assert agent.department == "marketing"

    def test_role_attribute(self):
        agent = AdCopywriter()
        assert agent.role == "ad_copywriter"

    def test_has_system_prompt(self):
        agent = AdCopywriter()
        assert len(agent.system_prompt) > 0

    def test_has_generate_ad_set_method(self):
        agent = AdCopywriter()
        assert callable(getattr(agent, "generate_ad_set", None))

    def test_generate_ad_set_no_api_returns_list(self):
        agent = AdCopywriter()
        result = agent.generate_ad_set({})
        assert isinstance(result, (list, dict))

    def test_generate_ad_set_with_product(self, sample_product):
        agent = AdCopywriter()
        result = agent.generate_ad_set(sample_product)
        assert isinstance(result, (list, dict))

    def test_generate_ad_set_with_goal(self, sample_product):
        agent = AdCopywriter()
        result = agent.generate_ad_set(sample_product, goal="регистрации")
        assert isinstance(result, (list, dict))

    def test_generate_ad_set_mocked_response(self, sample_product):
        agent = AdCopywriter()
        mock_ads = [
            {"platform": "yandex", "headline": "Модели для вашего события",
             "text": "Профессиональные модели", "cta": "Заказать", "usp": "Быстро и надёжно"}
        ]
        with patch.object(agent, "think_json", return_value=mock_ads):
            result = agent.generate_ad_set(sample_product, goal="заявки")
        assert result == mock_ads
        assert result[0]["platform"] == "yandex"


# ══════════════════════════════════════════════════════════════
# TestGrowthHacker
# ══════════════════════════════════════════════════════════════

class TestGrowthHacker:
    """GrowthHacker — unit tests."""

    def test_instantiation(self):
        agent = GrowthHacker()
        assert agent is not None

    def test_department_attribute(self):
        agent = GrowthHacker()
        assert agent.department == "marketing"

    def test_role_attribute(self):
        agent = GrowthHacker()
        assert agent.role == "growth_hacker"

    def test_has_system_prompt(self):
        agent = GrowthHacker()
        assert len(agent.system_prompt) > 0

    def test_has_generate_growth_experiments_method(self):
        agent = GrowthHacker()
        assert callable(getattr(agent, "generate_growth_experiments", None))

    def test_generate_growth_experiments_no_api_returns_list(self):
        agent = GrowthHacker()
        result = agent.generate_growth_experiments({})
        assert isinstance(result, (list, dict))

    def test_generate_growth_experiments_with_insights(self, sample_insights):
        agent = GrowthHacker()
        result = agent.generate_growth_experiments(sample_insights)
        assert isinstance(result, (list, dict))

    def test_generate_growth_experiments_with_count(self):
        agent = GrowthHacker()
        result = agent.generate_growth_experiments({}, count=10)
        assert isinstance(result, (list, dict))

    def test_generate_growth_experiments_mocked_response(self, sample_insights):
        agent = GrowthHacker()
        mock_exps = [
            {"name": "Telegram рассылка", "hypothesis": "Повысит конверсию на 15%",
             "channel": "telegram", "effort": "low", "expected_lift": "15%",
             "how_to_test": "A/B тест", "success_metric": "CTR > 5%"}
        ]
        with patch.object(agent, "think_json", return_value=mock_exps):
            result = agent.generate_growth_experiments(sample_insights, count=1)
        assert result == mock_exps
        assert result[0]["effort"] == "low"


# ══════════════════════════════════════════════════════════════
# TestMarketingDepartmentInstantiation
# ══════════════════════════════════════════════════════════════

class TestMarketingDepartmentInstantiation:
    """MarketingDepartment — construction and wiring."""

    def test_instantiation(self):
        d = MarketingDepartment()
        assert d is not None

    def test_has_content_agent(self):
        d = MarketingDepartment()
        assert isinstance(d.content, ContentStrategist)

    def test_has_viral_agent(self):
        d = MarketingDepartment()
        assert isinstance(d.viral, ViralEngineer)

    def test_has_seo_agent(self):
        d = MarketingDepartment()
        assert isinstance(d.seo, SEOSpecialist)

    def test_has_copywriter_agent(self):
        d = MarketingDepartment()
        assert isinstance(d.copywriter, AdCopywriter)

    def test_has_growth_agent(self):
        d = MarketingDepartment()
        assert isinstance(d.growth, GrowthHacker)

    def test_all_sub_agents_are_marketing_dept(self):
        d = MarketingDepartment()
        for agent in [d.content, d.viral, d.seo, d.copywriter, d.growth]:
            assert agent.department == "marketing"


# ══════════════════════════════════════════════════════════════
# TestMarketingDepartmentExecuteTask — no API (returns [] / {})
# ══════════════════════════════════════════════════════════════

class TestMarketingDepartmentExecuteTaskNoAPI:
    """execute_task with no real API key — all sub-calls return empty, result is []."""

    def test_execute_task_returns_list(self, dept, sample_insights):
        result = dept.execute_task({"action": "content_social"}, sample_insights)
        assert isinstance(result, list)

    def test_execute_task_content_empty_api(self, dept):
        """content action with empty API → plan is [] → no items saved."""
        result = dept.execute_task({"action": "content"}, {})
        assert isinstance(result, list)

    def test_execute_task_viral_empty_api(self, dept):
        result = dept.execute_task({"action": "viral"}, {})
        assert isinstance(result, list)

    def test_execute_task_seo_empty_api(self, dept):
        result = dept.execute_task({"action": "seo"}, {})
        assert isinstance(result, list)

    def test_execute_task_growth_empty_api(self, dept):
        result = dept.execute_task({"action": "growth"}, {})
        assert isinstance(result, list)

    def test_execute_task_unknown_action_empty_list(self, dept):
        result = dept.execute_task({"action": "unknown_xyz"}, {})
        assert result == []

    def test_execute_task_missing_action_key(self, dept):
        result = dept.execute_task({}, {})
        assert isinstance(result, list)

    def test_execute_task_product_id_none(self, dept):
        result = dept.execute_task({"action": "content"}, {}, product_id=None)
        assert isinstance(result, list)

    def test_execute_task_experiment_alias(self, dept):
        result = dept.execute_task({"action": "experiment"}, {})
        assert isinstance(result, list)

    def test_execute_task_tiktok_alias(self, dept):
        result = dept.execute_task({"action": "tiktok"}, {})
        assert isinstance(result, list)

    def test_execute_task_video_alias(self, dept):
        result = dept.execute_task({"action": "video"}, {})
        assert isinstance(result, list)

    def test_execute_task_social_alias(self, dept):
        result = dept.execute_task({"action": "social"}, {})
        assert isinstance(result, list)


# ══════════════════════════════════════════════════════════════
# TestMarketingDepartmentExecuteTask — with mocked sub-agents
# ══════════════════════════════════════════════════════════════

class TestMarketingDepartmentExecuteTaskMocked:
    """execute_task with mocked sub-agent responses."""

    @pytest.fixture
    def mock_content_plan(self):
        return [
            {"day": "Пн", "platform": "instagram", "topic": "Backstage", "format": "пост",
             "hook": "Посмотри как мы работаем", "cta": "Оставить заявку"},
            {"day": "Ср", "platform": "telegram", "topic": "Кейс", "format": "рассылка",
             "hook": "Как мы помогли бренду", "cta": "Узнать детали"},
        ]

    @pytest.fixture
    def mock_scripts(self):
        return [
            {"title": "День из жизни", "hook": "Вы не поверите", "script": "...",
             "hashtags": ["#models"], "music_style": "pop", "duration_sec": 30},
            {"title": "Реакция заказчика", "hook": "Когда видишь результат", "script": "...",
             "hashtags": ["#nevesty"], "music_style": "hype", "duration_sec": 15},
        ]

    @pytest.fixture
    def mock_seo_cluster(self):
        return {
            "main_keyword": "аренда моделей для мероприятий",
            "volume": "5000/мес",
            "pillar_article": {
                "title": "Аренда моделей — полный гайд",
                "meta": "Как выбрать модель для события",
                "h2s": ["Типы моделей", "Цены"]
            },
            "cluster_articles": [{"title": "Модели для корпоратива", "keyword": "модели корпоратив"}],
            "internal_links": ["/catalog"],
            "schema_type": "LocalBusiness"
        }

    @pytest.fixture
    def mock_growth_exps(self):
        return [
            {"name": "Telegram автоворонка", "hypothesis": "+20% конверсия",
             "channel": "telegram", "effort": "medium", "expected_lift": "20%",
             "how_to_test": "Запустить на 100 лидах", "success_metric": "Конверсия в заявку"},
            {"name": "Партнёрство с ивент-агентствами", "hypothesis": "+15 лидов/мес",
             "channel": "direct", "effort": "high", "expected_lift": "15 leads",
             "how_to_test": "Договориться с 3 агентствами", "success_metric": "Лиды от партнёров"},
        ]

    def test_content_task_saves_actions(self, dept, mock_content_plan, sample_insights):
        with patch.object(dept.content, "create_content_plan", return_value=mock_content_plan):
            result = dept.execute_task({"action": "content"}, sample_insights, product_id=1)
        assert len(result) == 2
        assert result[0]["type"] == "content_plan"

    def test_content_action_items_have_db_id(self, dept, mock_content_plan, sample_insights):
        with patch.object(dept.content, "create_content_plan", return_value=mock_content_plan):
            result = dept.execute_task({"action": "content"}, sample_insights, product_id=1)
        for item in result:
            assert "_db_id" in item

    def test_content_action_caps_at_five(self, dept, sample_insights):
        """Only first 5 items of content plan are saved."""
        big_plan = [
            {"day": f"День {i}", "platform": "instagram", "topic": f"Тема {i}",
             "format": "пост", "hook": "Hook", "cta": "CTA"}
            for i in range(10)
        ]
        with patch.object(dept.content, "create_content_plan", return_value=big_plan):
            result = dept.execute_task({"action": "content"}, sample_insights, product_id=1)
        assert len(result) == 5

    def test_viral_task_saves_scripts(self, dept, mock_scripts, sample_insights):
        with patch.object(dept.viral, "generate_video_scripts", return_value=mock_scripts):
            result = dept.execute_task({"action": "viral"}, sample_insights, product_id=2)
        assert len(result) == 2
        assert all(item["type"] == "viral_script" for item in result)

    def test_seo_task_saves_cluster(self, dept, mock_seo_cluster, sample_insights):
        with patch.object(dept.seo, "generate_seo_cluster", return_value=mock_seo_cluster):
            result = dept.execute_task({"action": "seo"}, sample_insights, product_id=3)
        assert len(result) == 1
        assert result[0]["type"] == "seo_cluster"
        assert result[0]["main_keyword"] == "аренда моделей для мероприятий"

    def test_growth_task_saves_experiments(self, dept, mock_growth_exps, sample_insights):
        with patch.object(dept.growth, "generate_growth_experiments", return_value=mock_growth_exps):
            result = dept.execute_task({"action": "growth"}, sample_insights, product_id=4)
        assert len(result) == 2
        assert all(item["type"] == "growth_experiment" for item in result)

    def test_growth_experiments_have_db_id(self, dept, mock_growth_exps, sample_insights):
        with patch.object(dept.growth, "generate_growth_experiments", return_value=mock_growth_exps):
            result = dept.execute_task({"action": "growth"}, sample_insights, product_id=4)
        for item in result:
            assert "_db_id" in item

    def test_combined_content_viral_action(self, dept, mock_content_plan, mock_scripts, sample_insights):
        """Action containing both 'content' and 'viral' triggers both handlers."""
        with patch.object(dept.content, "create_content_plan", return_value=mock_content_plan), \
             patch.object(dept.viral, "generate_video_scripts", return_value=mock_scripts):
            result = dept.execute_task({"action": "content_viral_social"}, sample_insights, product_id=5)
        types = [item["type"] for item in result]
        assert "content_plan" in types
        assert "viral_script" in types

    def test_seo_empty_cluster_not_saved(self, dept, sample_insights):
        """Empty SEO cluster ({}) should not be saved."""
        with patch.object(dept.seo, "generate_seo_cluster", return_value={}):
            result = dept.execute_task({"action": "seo"}, sample_insights)
        assert result == []

    def test_execute_task_product_id_passed_to_db(self, dept, mock_content_plan, sample_insights):
        """Ensure product_id is propagated (no TypeError)."""
        with patch.object(dept.content, "create_content_plan", return_value=mock_content_plan):
            result = dept.execute_task({"action": "content"}, sample_insights, product_id=99)
        assert all("_db_id" in item for item in result)
