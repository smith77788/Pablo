"""
Tests for factory/agents/product_dept.py — Product Department (БЛОК 5.2).

Covers:
  - ProductStrategist : department, run(), define_product_roadmap() (heuristic stub)
  - UXDesigner        : department, run(), audit_user_flow() (heuristic stub)
  - FunnelArchitect   : department, run(), design_funnel() (heuristic stub)
  - LandingBuilder    : department, run(), build_landing_structure() (heuristic stub)
  - ProductDepartment : instantiation, execute_task() with various task types
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from factory.agents.product_dept import (
    ProductStrategist,
    UXDesigner,
    FunnelArchitect,
    LandingBuilder,
    ProductDepartment,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def strategist() -> ProductStrategist:
    return ProductStrategist()


@pytest.fixture
def ux() -> UXDesigner:
    return UXDesigner()


@pytest.fixture
def funnel() -> FunnelArchitect:
    return FunnelArchitect()


@pytest.fixture
def landing() -> LandingBuilder:
    return LandingBuilder()


@pytest.fixture
def dept() -> ProductDepartment:
    return ProductDepartment()


@pytest.fixture
def sample_insights() -> dict:
    return {"orders_this_month": 15, "conversion_rate": 0.12}


@pytest.fixture
def sample_product() -> dict:
    return {"id": 42, "name": "Тариф «Премиум»", "price": 15000}


# ══════════════════════════════════════════════════════════════════════════════
# TestProductStrategist
# ══════════════════════════════════════════════════════════════════════════════

class TestProductStrategist:

    def test_instantiation(self, strategist):
        assert strategist is not None

    def test_department_attribute(self, strategist):
        assert strategist.department == "product"

    def test_role_attribute(self, strategist):
        assert strategist.role == "product_strategist"

    def test_run_returns_dict(self, strategist):
        result = strategist.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self, strategist):
        result = strategist.run({})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_run_insights_are_strings(self, strategist):
        result = strategist.run({})
        assert all(isinstance(i, str) for i in result["insights"])

    def test_run_has_recommendations(self, strategist):
        result = strategist.run({})
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_run_has_timestamp(self, strategist):
        result = strategist.run({})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_run_with_none_context(self, strategist):
        result = strategist.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_run_with_kpis(self, strategist):
        ctx = {"nevesty_kpis": {"orders_this_month": 20}}
        result = strategist.run(ctx)
        assert isinstance(result, dict)
        assert len(result["insights"]) > 0

    def test_define_product_roadmap_returns_dict(self, strategist):
        """Without a real API key, think_json returns {} — that is acceptable."""
        result = strategist.define_product_roadmap({})
        assert isinstance(result, dict)

    def test_define_product_roadmap_accepts_horizon(self, strategist):
        result = strategist.define_product_roadmap({}, horizon="90 дней")
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# TestUXDesigner
# ══════════════════════════════════════════════════════════════════════════════

class TestUXDesigner:

    def test_instantiation(self, ux):
        assert ux is not None

    def test_department_attribute(self, ux):
        assert ux.department == "product"

    def test_role_attribute(self, ux):
        assert ux.role == "ux_designer"

    def test_run_returns_dict(self, ux):
        result = ux.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self, ux):
        result = ux.run({})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_run_insights_are_strings(self, ux):
        result = ux.run({})
        assert all(isinstance(i, str) for i in result["insights"])

    def test_run_has_recommendations(self, ux):
        result = ux.run({})
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_run_has_timestamp(self, ux):
        result = ux.run({})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_run_with_none_context(self, ux):
        result = ux.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_audit_user_flow_returns_dict(self, ux):
        """Without a real API key, think_json returns {} — acceptable."""
        result = ux.audit_user_flow("booking", ["start", "catalog", "form"])
        assert isinstance(result, dict)

    def test_audit_user_flow_accepts_empty_steps(self, ux):
        result = ux.audit_user_flow("checkout", [])
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# TestFunnelArchitect
# ══════════════════════════════════════════════════════════════════════════════

class TestFunnelArchitect:

    def test_instantiation(self, funnel):
        assert funnel is not None

    def test_department_attribute(self, funnel):
        assert funnel.department == "product"

    def test_role_attribute(self, funnel):
        assert funnel.role == "funnel_architect"

    def test_run_returns_dict(self, funnel):
        result = funnel.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self, funnel):
        result = funnel.run({})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_run_insights_are_strings(self, funnel):
        result = funnel.run({})
        assert all(isinstance(i, str) for i in result["insights"])

    def test_run_has_recommendations(self, funnel):
        result = funnel.run({})
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_run_has_timestamp(self, funnel):
        result = funnel.run({})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_run_with_none_context(self, funnel):
        result = funnel.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_design_funnel_returns_dict(self, funnel):
        """Without a real API key, think_json returns {} — acceptable."""
        result = funnel.design_funnel("бронирование", "B2B организаторы")
        assert isinstance(result, dict)

    def test_design_funnel_with_different_audience(self, funnel):
        result = funnel.design_funnel("подписка", "частные лица")
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# TestLandingBuilder
# ══════════════════════════════════════════════════════════════════════════════

class TestLandingBuilder:

    def test_instantiation(self, landing):
        assert landing is not None

    def test_department_attribute(self, landing):
        assert landing.department == "product"

    def test_role_attribute(self, landing):
        assert landing.role == "landing_builder"

    def test_run_returns_dict(self, landing):
        result = landing.run({})
        assert isinstance(result, dict)

    def test_run_has_insights(self, landing):
        result = landing.run({})
        assert "insights" in result
        assert isinstance(result["insights"], list)
        assert len(result["insights"]) > 0

    def test_run_insights_are_strings(self, landing):
        result = landing.run({})
        assert all(isinstance(i, str) for i in result["insights"])

    def test_run_has_recommendations(self, landing):
        result = landing.run({})
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_run_has_timestamp(self, landing):
        result = landing.run({})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)

    def test_run_with_none_context(self, landing):
        result = landing.run(None)
        assert isinstance(result, dict)
        assert "insights" in result

    def test_build_landing_structure_returns_dict(self, landing):
        """Without a real API key, think_json returns {} — acceptable."""
        result = landing.build_landing_structure({"name": "Тест"})
        assert isinstance(result, dict)

    def test_build_landing_structure_accepts_custom_goal(self, landing):
        result = landing.build_landing_structure({}, goal="покупка")
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# TestProductDepartmentInstantiation
# ══════════════════════════════════════════════════════════════════════════════

class TestProductDepartmentInstantiation:

    def test_instantiation(self, dept):
        assert dept is not None

    def test_has_strategist(self, dept):
        assert isinstance(dept.strategist, ProductStrategist)

    def test_has_ux(self, dept):
        assert isinstance(dept.ux, UXDesigner)

    def test_has_funnel(self, dept):
        assert isinstance(dept.funnel, FunnelArchitect)

    def test_has_landing(self, dept):
        assert isinstance(dept.landing, LandingBuilder)

    def test_sub_agents_have_product_department(self, dept):
        assert dept.strategist.department == "product"
        assert dept.ux.department == "product"
        assert dept.funnel.department == "product"
        assert dept.landing.department == "product"


# ══════════════════════════════════════════════════════════════════════════════
# TestProductDepartmentExecuteTask
# ══════════════════════════════════════════════════════════════════════════════

class TestProductDepartmentExecuteTask:
    """
    execute_task() dispatches based on keywords in task["action"] and returns
    a list of action dicts (possibly empty when think_json returns no data without API).
    """

    def _run(self, dept, action: str, insights: dict | None = None, product: dict | None = None):
        return dept.execute_task({"action": action}, insights or {}, product)

    def test_execute_task_returns_list(self, dept):
        result = self._run(dept, "roadmap")
        assert isinstance(result, list)

    def test_execute_task_empty_action_returns_list(self, dept):
        result = self._run(dept, "")
        assert isinstance(result, list)

    def test_execute_task_unknown_action_returns_empty_list(self, dept):
        result = self._run(dept, "unknown_xyz_action")
        assert result == []

    def test_execute_task_roadmap_triggers_strategist(self, dept, sample_insights, sample_product):
        """When think_json is mocked to return roadmap data, a saved action is produced."""
        mock_roadmap = {
            "goal": "рост бронирований",
            "must_have": ["каталог", "форма"],
            "nice_to_have": ["отзывы"],
            "kill": ["лишние поля"],
            "success_metric": "+20% заявок",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "roadmap", sample_insights, sample_product)
        assert len(result) >= 1
        assert result[0]["type"] == "roadmap"

    def test_execute_task_roadmap_action_has_goal(self, dept, sample_insights):
        mock_roadmap = {
            "goal": "увеличить конверсию",
            "must_have": ["быстрая форма"],
            "nice_to_have": [],
            "kill": [],
            "success_metric": "+10% бронирований",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "roadmap", sample_insights)
        assert result[0]["goal"] == "увеличить конверсию"

    def test_execute_task_iterate_triggers_strategist(self, dept, sample_insights):
        mock_roadmap = {
            "goal": "итерация",
            "must_have": ["тест"],
            "nice_to_have": [],
            "kill": [],
            "success_metric": "NPS",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "iterate product", sample_insights)
        assert any(r["type"] == "roadmap" for r in result)

    def test_execute_task_feature_triggers_strategist(self, dept, sample_insights):
        mock_roadmap = {
            "goal": "новая фича",
            "must_have": ["фильтры"],
            "nice_to_have": [],
            "kill": [],
            "success_metric": "DAU",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "add feature", sample_insights)
        assert any(r["type"] == "roadmap" for r in result)

    def test_execute_task_ux_triggers_ux_designer(self, dept, sample_insights):
        mock_audit = {
            "pain_points": ["длинная форма"],
            "quick_wins": ["убрать поля"],
            "redesign_steps": ["шаг 1"],
            "expected_conversion_lift": "15%",
        }
        with patch.object(dept.ux, "think_json", return_value=mock_audit):
            result = self._run(dept, "ux audit", sample_insights)
        assert any(r["type"] == "ux_audit" for r in result)

    def test_execute_task_conversion_triggers_ux_designer(self, dept, sample_insights):
        mock_audit = {
            "pain_points": ["нет CTA"],
            "quick_wins": ["добавить кнопку"],
            "redesign_steps": [],
            "expected_conversion_lift": "10%",
        }
        with patch.object(dept.ux, "think_json", return_value=mock_audit):
            result = self._run(dept, "increase conversion", sample_insights)
        assert any(r["type"] == "ux_audit" for r in result)

    def test_execute_task_optimize_triggers_ux_designer(self, dept, sample_insights):
        mock_audit = {
            "pain_points": ["медленный сайт"],
            "quick_wins": ["оптимизировать изображения"],
            "redesign_steps": [],
            "expected_conversion_lift": "5%",
        }
        with patch.object(dept.ux, "think_json", return_value=mock_audit):
            result = self._run(dept, "optimize flow", sample_insights)
        assert any(r["type"] == "ux_audit" for r in result)

    def test_execute_task_funnel_triggers_funnel_architect(self, dept, sample_insights):
        mock_funnel = {
            "stages": [{"name": "Интерес", "message": "text", "cta": "кнопка", "drop_risk": "low"}],
            "total_steps": 3,
            "estimated_conversion": "12%",
        }
        with patch.object(dept.funnel, "think_json", return_value=mock_funnel):
            result = self._run(dept, "build funnel", sample_insights)
        assert any(r["type"] == "funnel" for r in result)

    def test_execute_task_landing_triggers_funnel_architect(self, dept, sample_insights):
        mock_funnel = {
            "stages": [{"name": "Hero", "message": "заголовок", "cta": "кнопка", "drop_risk": "low"}],
            "total_steps": 4,
            "estimated_conversion": "8%",
        }
        with patch.object(dept.funnel, "think_json", return_value=mock_funnel):
            result = self._run(dept, "create landing", sample_insights)
        assert any(r["type"] == "funnel" for r in result)

    def test_execute_task_roadmap_without_must_have_skips_save(self, dept, sample_insights):
        """If must_have is empty/absent, no db action should be saved for roadmap."""
        mock_roadmap = {"goal": "нет фич", "must_have": [], "nice_to_have": [], "kill": [], "success_metric": ""}
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "roadmap", sample_insights)
        assert all(r.get("type") != "roadmap" for r in result)

    def test_execute_task_ux_without_quick_wins_skips_save(self, dept, sample_insights):
        """If quick_wins is empty, no db action should be saved for ux_audit."""
        mock_audit = {"pain_points": [], "quick_wins": [], "redesign_steps": [], "expected_conversion_lift": "0%"}
        with patch.object(dept.ux, "think_json", return_value=mock_audit):
            result = self._run(dept, "ux audit", sample_insights)
        assert all(r.get("type") != "ux_audit" for r in result)

    def test_execute_task_funnel_without_stages_skips_save(self, dept, sample_insights):
        """If stages is empty, no db action should be saved for funnel."""
        mock_funnel = {"stages": [], "total_steps": 0, "estimated_conversion": "0%"}
        with patch.object(dept.funnel, "think_json", return_value=mock_funnel):
            result = self._run(dept, "funnel", sample_insights)
        assert all(r.get("type") != "funnel" for r in result)

    def test_execute_task_with_product_id(self, dept, sample_insights, sample_product):
        mock_roadmap = {
            "goal": "с продуктом",
            "must_have": ["фича"],
            "nice_to_have": [],
            "kill": [],
            "success_metric": "KPI",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap):
            result = self._run(dept, "roadmap", sample_insights, sample_product)
        assert len(result) >= 1
        # _db_id key present means db.insert was called
        assert "_db_id" in result[0]

    def test_execute_task_combined_roadmap_and_ux(self, dept, sample_insights):
        """Task with both 'roadmap' and 'ux' keywords should run both agents."""
        mock_roadmap = {
            "goal": "combo goal",
            "must_have": ["item"],
            "nice_to_have": [],
            "kill": [],
            "success_metric": "X",
        }
        mock_audit = {
            "pain_points": ["issue"],
            "quick_wins": ["fix"],
            "redesign_steps": [],
            "expected_conversion_lift": "5%",
        }
        with patch.object(dept.strategist, "think_json", return_value=mock_roadmap), \
             patch.object(dept.ux, "think_json", return_value=mock_audit):
            result = self._run(dept, "roadmap_ux_optimize", sample_insights)
        types = [r["type"] for r in result]
        assert "roadmap" in types
        assert "ux_audit" in types

    def test_execute_task_funnel_action_has_db_id(self, dept, sample_insights):
        mock_funnel = {
            "stages": [{"name": "Осведомлённость", "message": "msg", "cta": "go", "drop_risk": "low"}],
            "total_steps": 1,
            "estimated_conversion": "10%",
        }
        with patch.object(dept.funnel, "think_json", return_value=mock_funnel):
            result = self._run(dept, "funnel", sample_insights)
        assert any("_db_id" in r for r in result)
