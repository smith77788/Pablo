"""Tests for Operations Department agents (БЛОК 5.2).

Covers:
  - WorkflowManager    : department attr, optimize_workflow
  - AutomationBuilder  : department attr, find_automation_opportunities
  - CRMSpecialist      : department attr, analyze_client_data
  - TaskScheduler      : department attr, schedule_weekly_tasks
  - SystemOptimizer    : department attr, analyze_bottlenecks
  - OperationsDepartment : execute_task dispatcher (keyword routing)
"""
from __future__ import annotations

import pytest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from factory.agents.operations_dept import (
    WorkflowManager,
    AutomationBuilder,
    CRMSpecialist,
    TaskScheduler,
    SystemOptimizer,
    OperationsDepartment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_JSON_RESPONSE = '{"key": "value", "status": "ok"}'


def _make_dept() -> OperationsDepartment:
    return OperationsDepartment()


# ---------------------------------------------------------------------------
# WorkflowManager
# ---------------------------------------------------------------------------

class TestWorkflowManager:
    def setup_method(self):
        self.agent = WorkflowManager()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "operations"

    def test_role_attribute(self):
        assert self.agent.role == "workflow_manager"

    def test_optimize_workflow_returns_dict_no_api(self):
        """Without a real API key think() returns '' → think_json returns {}."""
        result = self.agent.optimize_workflow({})
        assert isinstance(result, dict)

    def test_optimize_workflow_none_context_no_crash(self):
        result = self.agent.optimize_workflow(None)
        assert isinstance(result, dict)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "bottlenecks": ["slow sign-off"],
        "improvements": [{"process": "booking", "impact": "высокий"}],
        "quick_wins": ["automate reminders"],
        "estimated_time_saved_hrs": 8,
    })
    def test_optimize_workflow_returns_mocked_data(self, mock_think):
        result = self.agent.optimize_workflow({"context": "test"})
        assert "bottlenecks" in result
        assert isinstance(result["bottlenecks"], list)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "quick_wins": ["win1", "win2"],
        "estimated_time_saved_hrs": 5,
    })
    def test_optimize_workflow_quick_wins_list(self, mock_think):
        result = self.agent.optimize_workflow({})
        assert "quick_wins" in result
        assert isinstance(result["quick_wins"], list)

    @patch('factory.agents.base.FactoryAgent.think_json', side_effect=Exception("API error"))
    def test_optimize_workflow_handles_exception(self, mock_think):
        result = self.agent.optimize_workflow({})
        assert result == {}


# ---------------------------------------------------------------------------
# AutomationBuilder
# ---------------------------------------------------------------------------

class TestAutomationBuilder:
    def setup_method(self):
        self.agent = AutomationBuilder()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "operations"

    def test_role_attribute(self):
        assert self.agent.role == "automation_builder"

    def test_find_automation_opportunities_returns_dict_no_api(self):
        result = self.agent.find_automation_opportunities({})
        assert isinstance(result, dict)

    def test_find_automation_opportunities_none_context_no_crash(self):
        result = self.agent.find_automation_opportunities(None)
        assert isinstance(result, dict)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "automation_opportunities": [
            {"task": "lead intake", "automation_type": "telegram_bot", "roi": "высокий"},
        ],
        "priority_automation": "lead intake",
        "estimated_savings_hrs_per_week": 12,
    })
    def test_find_automation_opportunities_mocked(self, mock_think):
        result = self.agent.find_automation_opportunities({})
        assert "automation_opportunities" in result
        assert isinstance(result["automation_opportunities"], list)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "estimated_savings_hrs_per_week": 10,
    })
    def test_find_automation_savings_positive(self, mock_think):
        result = self.agent.find_automation_opportunities({})
        assert result.get("estimated_savings_hrs_per_week", 0) >= 0

    @patch('factory.agents.base.FactoryAgent.think_json', side_effect=Exception("timeout"))
    def test_find_automation_handles_exception(self, mock_think):
        result = self.agent.find_automation_opportunities({})
        assert result == {}


# ---------------------------------------------------------------------------
# CRMSpecialist
# ---------------------------------------------------------------------------

class TestCRMSpecialist:
    def setup_method(self):
        self.agent = CRMSpecialist()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "operations"

    def test_role_attribute(self):
        assert self.agent.role == "crm_specialist"

    def test_analyze_client_data_returns_dict_no_api(self):
        result = self.agent.analyze_client_data({})
        assert isinstance(result, dict)

    def test_analyze_client_data_none_context_no_crash(self):
        result = self.agent.analyze_client_data(None)
        assert isinstance(result, dict)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "client_segments": [{"name": "VIP", "criteria": "budget > 50000", "strategy": "personal manager"}],
        "retention_tactics": ["loyalty discount", "birthday offer"],
        "repeat_order_trigger": "90 days after last order",
    })
    def test_analyze_client_data_mocked_segments(self, mock_think):
        result = self.agent.analyze_client_data({"clients": 100})
        assert "client_segments" in result
        assert len(result["client_segments"]) >= 1

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "retention_tactics": ["tactic1"],
    })
    def test_analyze_client_data_retention_list(self, mock_think):
        result = self.agent.analyze_client_data({})
        assert isinstance(result.get("retention_tactics", []), list)

    @patch('factory.agents.base.FactoryAgent.think_json', side_effect=Exception("network error"))
    def test_analyze_client_data_handles_exception(self, mock_think):
        result = self.agent.analyze_client_data({})
        assert result == {}


# ---------------------------------------------------------------------------
# TaskScheduler
# ---------------------------------------------------------------------------

class TestTaskScheduler:
    def setup_method(self):
        self.agent = TaskScheduler()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "operations"

    def test_role_attribute(self):
        assert self.agent.role == "task_scheduler"

    def test_schedule_weekly_tasks_returns_dict_no_api(self):
        result = self.agent.schedule_weekly_tasks({})
        assert isinstance(result, dict)

    def test_schedule_weekly_tasks_none_context_no_crash(self):
        result = self.agent.schedule_weekly_tasks(None)
        assert isinstance(result, dict)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "week_tasks": [
            {"day": "Пн", "priority": 1, "task": "Respond to leads", "duration_hrs": 2},
        ],
        "week_focus": "Increase conversions",
        "blockers": ["Missing model photos"],
        "success_criteria": "5 new bookings",
    })
    def test_schedule_weekly_tasks_mocked(self, mock_think):
        result = self.agent.schedule_weekly_tasks({})
        assert "week_tasks" in result
        assert isinstance(result["week_tasks"], list)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "week_focus": "Revenue growth",
        "success_criteria": "Hit monthly target",
    })
    def test_schedule_weekly_tasks_has_focus(self, mock_think):
        result = self.agent.schedule_weekly_tasks({})
        assert isinstance(result.get("week_focus", ""), str)

    @patch('factory.agents.base.FactoryAgent.think_json', side_effect=Exception("parse error"))
    def test_schedule_weekly_tasks_handles_exception(self, mock_think):
        result = self.agent.schedule_weekly_tasks({})
        assert result == {}


# ---------------------------------------------------------------------------
# SystemOptimizer
# ---------------------------------------------------------------------------

class TestSystemOptimizer:
    def setup_method(self):
        self.agent = SystemOptimizer()

    def test_instantiation(self):
        assert self.agent is not None

    def test_department_attribute(self):
        assert self.agent.department == "operations"

    def test_role_attribute(self):
        assert self.agent.role == "system_optimizer"

    def test_analyze_bottlenecks_returns_dict_no_api(self):
        result = self.agent.analyze_bottlenecks({})
        assert isinstance(result, dict)

    def test_analyze_bottlenecks_none_context_no_crash(self):
        result = self.agent.analyze_bottlenecks(None)
        assert isinstance(result, dict)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "bottlenecks": [
            {"area": "response time", "problem": "slow reply", "solution": "bot auto-reply", "impact": "высокий"},
        ],
        "system_health": "удовлетворительное",
        "top_priority_fix": "auto-reply to new leads",
        "monitoring_metrics": ["response_time_min"],
    })
    def test_analyze_bottlenecks_mocked(self, mock_think):
        result = self.agent.analyze_bottlenecks({})
        assert "bottlenecks" in result
        assert isinstance(result["bottlenecks"], list)

    @patch('factory.agents.base.FactoryAgent.think_json', return_value={
        "system_health": "хорошее",
    })
    def test_analyze_bottlenecks_health_field(self, mock_think):
        result = self.agent.analyze_bottlenecks({"metrics": {}})
        assert "system_health" in result

    @patch('factory.agents.base.FactoryAgent.think_json', side_effect=Exception("json error"))
    def test_analyze_bottlenecks_handles_exception(self, mock_think):
        result = self.agent.analyze_bottlenecks({})
        assert result == {}


# ---------------------------------------------------------------------------
# OperationsDepartment — keyword routing + output shape
# ---------------------------------------------------------------------------

class TestOperationsDepartment:
    def test_instantiation(self):
        dept = _make_dept()
        assert dept is not None

    def test_has_all_agent_instances(self):
        dept = _make_dept()
        assert isinstance(dept.workflow, WorkflowManager)
        assert isinstance(dept.automation, AutomationBuilder)
        assert isinstance(dept.crm, CRMSpecialist)
        assert isinstance(dept.scheduler, TaskScheduler)
        assert isinstance(dept.optimizer, SystemOptimizer)

    def test_execute_task_returns_dict(self):
        result = _make_dept().execute_task("optimize workflow", {})
        assert isinstance(result, dict)

    def test_execute_task_has_department_field(self):
        result = _make_dept().execute_task("optimize", {})
        assert result.get("department") == "operations"

    def test_execute_task_has_task_field(self):
        result = _make_dept().execute_task("my task", {})
        assert result.get("task") == "my task"

    def test_execute_task_has_roles_used_list(self):
        result = _make_dept().execute_task("optimize workflow", {})
        assert "roles_used" in result
        assert isinstance(result["roles_used"], list)

    def test_execute_task_has_timestamp(self):
        result = _make_dept().execute_task("test task", {})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)
        assert "T" in result["timestamp"]  # ISO format

    def test_execute_task_has_result_dict(self):
        result = _make_dept().execute_task("optimize", {})
        assert "result" in result
        assert isinstance(result["result"], dict)

    def test_execute_task_none_context_no_crash(self):
        result = _make_dept().execute_task("test", None)
        assert isinstance(result, dict)
        assert result.get("department") == "operations"

    def test_execute_task_empty_string_activates_optimizer(self):
        """Empty task string → no keyword match → fallback to system_optimizer."""
        result = _make_dept().execute_task("", {})
        assert "system_optimizer" in result["roles_used"]

    def test_execute_task_workflow_keyword_activates_workflow_manager(self):
        result = _make_dept().execute_task("workflow analysis", {})
        assert "workflow_manager" in result["roles_used"]

    def test_execute_task_optimize_keyword_activates_workflow_manager(self):
        result = _make_dept().execute_task("optimize our process", {})
        assert "workflow_manager" in result["roles_used"]

    def test_execute_task_automat_keyword_activates_automation_builder(self):
        result = _make_dept().execute_task("automate the bot tasks", {})
        assert "automation_builder" in result["roles_used"]

    def test_execute_task_crm_keyword_activates_crm_specialist(self):
        result = _make_dept().execute_task("crm data analysis", {})
        assert "crm_specialist" in result["roles_used"]

    def test_execute_task_client_keyword_activates_crm_specialist(self):
        result = _make_dept().execute_task("клиент retention strategy", {})
        assert "crm_specialist" in result["roles_used"]

    def test_execute_task_schedule_keyword_activates_task_scheduler(self):
        result = _make_dept().execute_task("schedule tasks for the week", {})
        assert "task_scheduler" in result["roles_used"]

    def test_execute_task_bottleneck_keyword_activates_optimizer(self):
        result = _make_dept().execute_task("find bottleneck in system", {})
        assert "system_optimizer" in result["roles_used"]

    def test_execute_task_health_keyword_activates_optimizer(self):
        result = _make_dept().execute_task("system health check", {})
        assert "system_optimizer" in result["roles_used"]

    def test_execute_task_multiple_keywords_activates_multiple_roles(self):
        result = _make_dept().execute_task("workflow automat crm client schedule plan", {})
        roles = result["roles_used"]
        assert len(roles) >= 2

    def test_execute_task_roles_used_len_ge_1(self):
        result = _make_dept().execute_task("any unknown query", {})
        assert len(result["roles_used"]) >= 1

    def test_execute_task_result_key_matches_role(self):
        """workflow keyword → result should have 'workflow' key."""
        result = _make_dept().execute_task("optimize workflow", {})
        assert "workflow" in result["result"]

    def test_execute_task_automation_result_key(self):
        result = _make_dept().execute_task("automate tasks", {})
        assert "automation" in result["result"]

    def test_execute_task_crm_result_key(self):
        result = _make_dept().execute_task("crm analysis", {})
        assert "crm" in result["result"]

    def test_execute_task_schedule_result_key(self):
        result = _make_dept().execute_task("schedule week plan", {})
        assert "schedule" in result["result"]

    def test_execute_task_optimization_result_key_when_fallback(self):
        result = _make_dept().execute_task("", {})
        assert "optimization" in result["result"]

    def test_execute_task_with_rich_context(self):
        ctx = {
            "nevesty_kpis": {"orders_this_month": 42, "revenue_month": 210000},
            "team_size": 5,
        }
        result = _make_dept().execute_task("optimize workflow process", ctx)
        assert isinstance(result, dict)
        assert result["department"] == "operations"

    def test_execute_task_cyrillic_keyword_процесс(self):
        result = _make_dept().execute_task("оптимиз процесс агентства", {})
        assert "workflow_manager" in result["roles_used"]

    def test_execute_task_cyrillic_keyword_автомат(self):
        result = _make_dept().execute_task("автомат ответ боту", {})
        assert "automation_builder" in result["roles_used"]

    def test_execute_task_cyrillic_keyword_план(self):
        result = _make_dept().execute_task("план задач на неделя", {})
        assert "task_scheduler" in result["roles_used"]

    def test_execute_task_no_duplicate_roles(self):
        result = _make_dept().execute_task("workflow optimize", {})
        roles = result["roles_used"]
        assert len(roles) == len(set(roles)), "roles_used should have no duplicates"
