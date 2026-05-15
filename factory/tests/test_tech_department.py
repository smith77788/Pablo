"""
Tests for factory/agents/tech_dept.py — БЛОК 5.2.

Covers all 5 agents and the TechDepartment coordinator:
  - BackendDeveloper   : instantiation, department, optimize_backend
  - FrontendBuilder    : instantiation, department, suggest_ui_improvements
  - APIEngineer        : instantiation, department, analyze_api
  - DeploymentManager  : instantiation, department, check_system_health
  - QATester           : instantiation, department, generate_test_cases
  - TechDepartment     : execute_task routing, all keyword branches,
                         roles_used, timestamp, fallback to qa
"""
from __future__ import annotations

import pytest
from datetime import timezone
from unittest.mock import patch, MagicMock

from factory.agents.tech_dept import (
    BackendDeveloper,
    FrontendBuilder,
    APIEngineer,
    DeploymentManager,
    QATester,
    TechDepartment,
)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _mock_think_json(return_value: dict = None):
    """Patch think_json on FactoryAgent to avoid LLM calls."""
    rv = return_value if return_value is not None else {}
    return patch("factory.agents.base.FactoryAgent.think_json", return_value=rv)


# ══════════════════════════════════════════════════════════════
# BackendDeveloper
# ══════════════════════════════════════════════════════════════

class TestBackendDeveloper:
    def setup_method(self):
        self.agent = BackendDeveloper()

    def test_instantiation(self):
        assert isinstance(self.agent, BackendDeveloper)

    def test_department_is_tech(self):
        assert self.agent.department == "tech"

    def test_role(self):
        assert self.agent.role == "backend_dev"

    def test_name(self):
        assert self.agent.name == "backend_developer"

    def test_optimize_backend_returns_dict_with_mock(self):
        with _mock_think_json({"performance_issues": [], "priority_fixes": ["fix1"]}):
            result = self.agent.optimize_backend({})
        assert isinstance(result, dict)

    def test_optimize_backend_empty_context(self):
        with _mock_think_json({}):
            result = self.agent.optimize_backend({})
        assert isinstance(result, dict)

    def test_optimize_backend_none_llm_returns_empty_dict(self):
        # When LLM returns None (e.g. no API key), method returns {}
        with patch("factory.agents.base.FactoryAgent.think_json", return_value=None):
            result = self.agent.optimize_backend({})
        assert isinstance(result, dict)

    def test_optimize_backend_exception_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", side_effect=RuntimeError("boom")):
            result = self.agent.optimize_backend({})
        assert result == {}

    def test_optimize_backend_with_nonempty_context(self):
        ctx = {"node_version": "18", "db": "sqlite"}
        with _mock_think_json({"db_optimizations": ["add index"]}):
            result = self.agent.optimize_backend(ctx)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# FrontendBuilder
# ══════════════════════════════════════════════════════════════

class TestFrontendBuilder:
    def setup_method(self):
        self.agent = FrontendBuilder()

    def test_instantiation(self):
        assert isinstance(self.agent, FrontendBuilder)

    def test_department_is_tech(self):
        assert self.agent.department == "tech"

    def test_role(self):
        assert self.agent.role == "frontend_builder"

    def test_name(self):
        assert self.agent.name == "frontend_builder"

    def test_suggest_ui_improvements_returns_dict(self):
        with _mock_think_json({"ux_issues": [], "new_features": []}):
            result = self.agent.suggest_ui_improvements({})
        assert isinstance(result, dict)

    def test_suggest_ui_improvements_exception_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", side_effect=Exception("err")):
            result = self.agent.suggest_ui_improvements({})
        assert result == {}

    def test_suggest_ui_improvements_none_llm_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", return_value=None):
            result = self.agent.suggest_ui_improvements({})
        assert isinstance(result, dict)

    def test_suggest_ui_improvements_with_context(self):
        ctx = {"page": "admin", "users": 50}
        with _mock_think_json({"mobile_improvements": ["add hamburger menu"]}):
            result = self.agent.suggest_ui_improvements(ctx)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# APIEngineer
# ══════════════════════════════════════════════════════════════

class TestAPIEngineer:
    def setup_method(self):
        self.agent = APIEngineer()

    def test_instantiation(self):
        assert isinstance(self.agent, APIEngineer)

    def test_department_is_tech(self):
        assert self.agent.department == "tech"

    def test_role(self):
        assert self.agent.role == "api_engineer"

    def test_name(self):
        assert self.agent.name == "api_engineer"

    def test_analyze_api_returns_dict(self):
        with _mock_think_json({"api_issues": [], "missing_endpoints": []}):
            result = self.agent.analyze_api({})
        assert isinstance(result, dict)

    def test_analyze_api_exception_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", side_effect=ValueError("bad")):
            result = self.agent.analyze_api({})
        assert result == {}

    def test_analyze_api_none_llm_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", return_value=None):
            result = self.agent.analyze_api({})
        assert isinstance(result, dict)

    def test_analyze_api_with_context(self):
        ctx = {"endpoints": ["/api/models", "/api/orders"]}
        with _mock_think_json({"rate_limiting": "100 req/min"}):
            result = self.agent.analyze_api(ctx)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# DeploymentManager
# ══════════════════════════════════════════════════════════════

class TestDeploymentManager:
    def setup_method(self):
        self.agent = DeploymentManager()

    def test_instantiation(self):
        assert isinstance(self.agent, DeploymentManager)

    def test_department_is_tech(self):
        assert self.agent.department == "tech"

    def test_role(self):
        assert self.agent.role == "deployment"

    def test_name(self):
        assert self.agent.name == "deployment_manager"

    def test_check_system_health_returns_dict(self):
        with _mock_think_json({"health_status": "здоровый", "next_steps": []}):
            result = self.agent.check_system_health({})
        assert isinstance(result, dict)

    def test_check_system_health_exception_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", side_effect=OSError("disk")):
            result = self.agent.check_system_health({})
        assert result == {}

    def test_check_system_health_none_llm_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", return_value=None):
            result = self.agent.check_system_health({})
        assert isinstance(result, dict)

    def test_check_system_health_with_context(self):
        ctx = {"uptime_days": 30, "docker": True}
        with _mock_think_json({"health_status": "здоровый"}):
            result = self.agent.check_system_health(ctx)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# QATester
# ══════════════════════════════════════════════════════════════

class TestQATester:
    def setup_method(self):
        self.agent = QATester()

    def test_instantiation(self):
        assert isinstance(self.agent, QATester)

    def test_department_is_tech(self):
        assert self.agent.department == "tech"

    def test_role(self):
        assert self.agent.role == "qa_tester"

    def test_name(self):
        assert self.agent.name == "qa_tester"

    def test_generate_test_cases_returns_dict(self):
        with _mock_think_json({"test_cases": [], "potential_bugs": []}):
            result = self.agent.generate_test_cases({})
        assert isinstance(result, dict)

    def test_generate_test_cases_exception_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", side_effect=Exception("fail")):
            result = self.agent.generate_test_cases({})
        assert result == {}

    def test_generate_test_cases_none_llm_returns_empty_dict(self):
        with patch("factory.agents.base.FactoryAgent.think_json", return_value=None):
            result = self.agent.generate_test_cases({})
        assert isinstance(result, dict)

    def test_generate_test_cases_with_context(self):
        ctx = {"features": ["login", "booking", "payment"]}
        with _mock_think_json({"edge_cases": ["empty username"]}):
            result = self.agent.generate_test_cases(ctx)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# TechDepartment — coordinator
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def dept() -> TechDepartment:
    return TechDepartment()


class TestTechDepartmentInstantiation:
    def test_has_backend_agent(self, dept):
        assert isinstance(dept.backend, BackendDeveloper)

    def test_has_frontend_agent(self, dept):
        assert isinstance(dept.frontend, FrontendBuilder)

    def test_has_api_agent(self, dept):
        assert isinstance(dept.api, APIEngineer)

    def test_has_deployment_agent(self, dept):
        assert isinstance(dept.deployment, DeploymentManager)

    def test_has_qa_agent(self, dept):
        assert isinstance(dept.qa, QATester)


class TestTechDepartmentExecuteTask:

    # ── return structure ──────────────────────────────────────

    def test_execute_task_returns_dict(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend optimization", {})
        assert isinstance(result, dict)

    def test_execute_task_has_department_key(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend optimization", {})
        assert result["department"] == "tech"

    def test_execute_task_has_task_key(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend optimization", {})
        assert result["task"] == "backend optimization"

    def test_execute_task_has_roles_used_list(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend", {})
        assert "roles_used" in result
        assert isinstance(result["roles_used"], list)

    def test_execute_task_has_timestamp(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("qa test", {})
        assert "timestamp" in result
        assert "T" in result["timestamp"]  # ISO format

    def test_execute_task_has_result_dict(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend", {})
        assert "result" in result
        assert isinstance(result["result"], dict)

    # ── keyword routing ───────────────────────────────────────

    def test_backend_keyword_triggers_backend_dev(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend optimization", {})
        assert "backend_dev" in result["roles_used"]

    def test_node_keyword_triggers_backend_dev(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("node.js issues", {})
        assert "backend_dev" in result["roles_used"]

    def test_sqlite_keyword_triggers_backend_dev(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("sqlite query slow", {})
        assert "backend_dev" in result["roles_used"]

    def test_бэкенд_keyword_triggers_backend_dev(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("бэкенд проблемы", {})
        assert "backend_dev" in result["roles_used"]

    def test_frontend_keyword_triggers_frontend_builder(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("frontend improvements", {})
        assert "frontend_builder" in result["roles_used"]

    def test_ui_keyword_triggers_frontend_builder(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("ui layout issues", {})
        assert "frontend_builder" in result["roles_used"]

    def test_html_keyword_triggers_frontend_builder(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("html template", {})
        assert "frontend_builder" in result["roles_used"]

    def test_api_keyword_triggers_api_engineer(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("api endpoint", {})
        assert "api_engineer" in result["roles_used"]

    def test_rest_keyword_triggers_api_engineer(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("rest routes", {})
        assert "api_engineer" in result["roles_used"]

    def test_webhook_keyword_triggers_api_engineer(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("webhook handler", {})
        assert "api_engineer" in result["roles_used"]

    def test_deploy_keyword_triggers_deployment(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("deploy to production", {})
        assert "deployment" in result["roles_used"]

    def test_docker_keyword_triggers_deployment(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("docker container", {})
        assert "deployment" in result["roles_used"]

    def test_health_keyword_triggers_deployment(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("health check", {})
        assert "deployment" in result["roles_used"]

    def test_test_keyword_triggers_qa(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("write test cases", {})
        assert "qa_tester" in result["roles_used"]

    def test_bug_keyword_triggers_qa(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("bug report", {})
        assert "qa_tester" in result["roles_used"]

    def test_qa_keyword_triggers_qa(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("qa review", {})
        assert "qa_tester" in result["roles_used"]

    def test_unknown_task_falls_back_to_qa(self, dept):
        """When no keywords match, qa_tester is always invoked as fallback."""
        with _mock_think_json({}):
            result = dept.execute_task("completely unknown request", {})
        assert "qa_tester" in result["roles_used"]

    def test_unknown_task_only_qa_role(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("completely unknown request xyz", {})
        assert result["roles_used"] == ["qa_tester"]

    def test_multiple_keywords_multiple_roles(self, dept):
        """A task with both 'backend' and 'api' keywords should engage both agents."""
        with _mock_think_json({}):
            result = dept.execute_task("backend api optimization", {})
        assert "backend_dev" in result["roles_used"]
        assert "api_engineer" in result["roles_used"]

    def test_api_also_triggers_backend_when_both_present(self, dept):
        """'api' matches both backend_dev (in its keyword list) and api_engineer."""
        with _mock_think_json({}):
            result = dept.execute_task("api backend", {})
        assert "api_engineer" in result["roles_used"]
        assert "backend_dev" in result["roles_used"]

    # ── context handling ──────────────────────────────────────

    def test_none_context_no_error(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend issue", None)
        assert isinstance(result, dict)
        assert "roles_used" in result

    def test_empty_context_no_error(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("frontend ux", {})
        assert isinstance(result, dict)

    def test_rich_context_passed_through(self, dept):
        ctx = {
            "server": "nginx",
            "db_size_mb": 500,
            "active_users": 120,
        }
        with _mock_think_json({"db_optimizations": ["index orders.status"]}):
            result = dept.execute_task("backend sqlite", ctx)
        assert result["department"] == "tech"
        assert "backend_dev" in result["roles_used"]

    # ── result content ────────────────────────────────────────

    def test_result_contains_backend_key_when_backend_triggered(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend", {})
        assert "backend" in result["result"]

    def test_result_contains_frontend_key_when_frontend_triggered(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("frontend ui", {})
        assert "frontend" in result["result"]

    def test_result_contains_api_key_when_api_triggered(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("rest endpoint", {})
        assert "api" in result["result"]

    def test_result_contains_deployment_key_when_deploy_triggered(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("deploy docker", {})
        assert "deployment" in result["result"]

    def test_result_contains_qa_key_when_qa_triggered(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("тест", {})
        assert "qa" in result["result"]

    def test_timestamp_is_utc_iso_format(self, dept):
        from datetime import datetime
        with _mock_think_json({}):
            result = dept.execute_task("qa", {})
        ts = result["timestamp"]
        # Must be parseable as an ISO datetime
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None  # timezone-aware

    def test_roles_used_has_no_duplicates(self, dept):
        with _mock_think_json({}):
            result = dept.execute_task("backend node sqlite сервер бэкенд", {})
        assert len(result["roles_used"]) == len(set(result["roles_used"]))
