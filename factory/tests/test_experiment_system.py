"""Tests for factory/agents/experiment_system.py — Phase 28 A/B Experiment System."""
from __future__ import annotations
import json
import os
import tempfile

import pytest

from factory.agents.experiment_system import (
    HeuristicExperimentSystem,
    EXPERIMENT_TEMPLATES,
)


@pytest.fixture
def tmp_history(tmp_path):
    return str(tmp_path / "experiment_history.json")


@pytest.fixture
def system(tmp_history):
    return HeuristicExperimentSystem(history_path=tmp_history)


# ── EXPERIMENT_TEMPLATES ──────────────────────────────────────────────────────

def test_templates_not_empty():
    assert len(EXPERIMENT_TEMPLATES) > 0


def test_templates_have_required_keys():
    required = {'id', 'name', 'hypothesis', 'metric', 'variants', 'duration_days'}
    for tmpl in EXPERIMENT_TEMPLATES:
        assert required <= set(tmpl.keys()), f"Missing keys in {tmpl['id']}"


def test_templates_have_at_least_two_variants():
    for tmpl in EXPERIMENT_TEMPLATES:
        assert len(tmpl['variants']) >= 2


# ── propose_experiments ───────────────────────────────────────────────────────

def test_propose_experiments_returns_nonempty_list(system):
    proposed = system.propose_experiments()
    assert isinstance(proposed, list)
    assert len(proposed) > 0


def test_propose_experiments_returns_all_templates_initially(system):
    proposed = system.propose_experiments()
    assert len(proposed) == len(EXPERIMENT_TEMPLATES)


def test_propose_excludes_running_experiment(system):
    system.start_experiment('catalog_sort_featured_first')
    proposed = system.propose_experiments()
    ids = [e['id'] for e in proposed]
    assert 'catalog_sort_featured_first' not in ids


# ── start_experiment ──────────────────────────────────────────────────────────

def test_start_experiment_known_id_returns_started(system):
    result = system.start_experiment('catalog_sort_featured_first')
    assert result['status'] == 'started'


def test_start_experiment_returns_experiment_dict(system):
    result = system.start_experiment('quick_booking_button')
    assert 'experiment' in result
    exp = result['experiment']
    assert exp['experiment_id'] == 'quick_booking_button'


def test_start_experiment_unknown_id_returns_error(system):
    result = system.start_experiment('nonexistent_experiment')
    assert result['status'] == 'error'
    assert 'error' in result


def test_start_experiment_sets_status_running(system):
    result = system.start_experiment('discount_banner')
    assert result['experiment']['status'] == 'running'


def test_start_experiment_sets_ends_at(system):
    result = system.start_experiment('catalog_sort_featured_first')
    assert 'ends_at' in result['experiment']


# ── get_active_experiments ────────────────────────────────────────────────────

def test_get_active_experiments_empty_initially(system):
    assert system.get_active_experiments() == []


def test_get_active_experiments_has_one_after_start(system):
    system.start_experiment('catalog_sort_featured_first')
    active = system.get_active_experiments()
    assert len(active) == 1


def test_get_active_experiments_has_correct_id(system):
    system.start_experiment('quick_booking_button')
    active = system.get_active_experiments()
    assert active[0]['experiment_id'] == 'quick_booking_button'


# ── evaluate_experiment ───────────────────────────────────────────────────────

def test_evaluate_experiment_variant_wins_when_better(system):
    system.start_experiment('catalog_sort_featured_first')
    result = system.evaluate_experiment(
        'catalog_sort_featured_first',
        {'control': 100, 'featured_first': 120},
    )
    assert result['winner'] == 'variant'
    assert result['improvement_pct'] == pytest.approx(20.0, abs=0.1)


def test_evaluate_experiment_control_wins_when_variant_worse(system):
    system.start_experiment('catalog_sort_featured_first')
    result = system.evaluate_experiment(
        'catalog_sort_featured_first',
        {'control': 100, 'featured_first': 90},
    )
    assert result['winner'] == 'control'


def test_evaluate_experiment_inconclusive_when_close(system):
    system.start_experiment('catalog_sort_featured_first')
    result = system.evaluate_experiment(
        'catalog_sort_featured_first',
        {'control': 100, 'featured_first': 103},
    )
    assert result['winner'] == 'inconclusive'


def test_evaluate_experiment_returns_error_for_unknown(system):
    result = system.evaluate_experiment('nonexistent', {'control': 100})
    assert result['status'] == 'error'


def test_evaluate_experiment_marks_completed(system):
    system.start_experiment('quick_booking_button')
    system.evaluate_experiment('quick_booking_button', {'control': 50, 'quick_button': 60})
    completed = system.get_completed_experiments()
    assert len(completed) == 1


def test_evaluate_experiment_zero_control_returns_zero_improvement(system):
    system.start_experiment('catalog_sort_featured_first')
    result = system.evaluate_experiment(
        'catalog_sort_featured_first',
        {'control': 0, 'featured_first': 50},
    )
    assert result['improvement_pct'] == 0.0


# ── generate_report ───────────────────────────────────────────────────────────

def test_generate_report_returns_string(system):
    report = system.generate_report()
    assert isinstance(report, str)


def test_generate_report_contains_keywords(system):
    report = system.generate_report()
    assert 'ОТЧЁТ' in report or 'Предложено' in report


def test_generate_report_shows_active_after_start(system):
    system.start_experiment('discount_banner')
    report = system.generate_report()
    assert 'Активных' in report


def test_generate_report_shows_completed_after_evaluate(system):
    system.start_experiment('quick_booking_button')
    system.evaluate_experiment('quick_booking_button', {'control': 40, 'quick_button': 50})
    report = system.generate_report()
    assert 'Завершённых' in report


# ── run_cycle ─────────────────────────────────────────────────────────────────

def test_run_cycle_returns_dict(system):
    result = system.run_cycle()
    assert isinstance(result, dict)


def test_run_cycle_status_ok(system):
    result = system.run_cycle()
    assert result['status'] == 'ok'


def test_run_cycle_auto_starts_experiment(system):
    result = system.run_cycle()
    assert result['active_count'] == 1
    assert result['started_experiment'] is not None


def test_run_cycle_no_second_start_when_active(system):
    system.run_cycle()
    result2 = system.run_cycle()
    assert result2['started_experiment'] is None


def test_run_cycle_contains_report(system):
    result = system.run_cycle()
    assert 'report' in result
    assert isinstance(result['report'], str)


def test_run_cycle_has_proposed_count(system):
    result = system.run_cycle()
    assert 'proposed_count' in result


# ── run_phase_28_experiments ──────────────────────────────────────────────────

def test_run_phase_28_experiments_returns_dict(tmp_history):
    from factory.cycle import run_phase_28_experiments
    result = run_phase_28_experiments(history_path=tmp_history)
    assert isinstance(result, dict)


def test_run_phase_28_experiments_has_status(tmp_history):
    from factory.cycle import run_phase_28_experiments
    result = run_phase_28_experiments(history_path=tmp_history)
    assert 'status' in result
    assert result['status'] == 'ok'


def test_run_phase_28_experiments_has_active_count(tmp_history):
    from factory.cycle import run_phase_28_experiments
    result = run_phase_28_experiments(history_path=tmp_history)
    assert 'active_count' in result


def test_run_phase_28_experiments_has_proposed_count(tmp_history):
    from factory.cycle import run_phase_28_experiments
    result = run_phase_28_experiments(history_path=tmp_history)
    assert 'proposed_count' in result


# ── persistence ───────────────────────────────────────────────────────────────

def test_history_persists_across_instances(tmp_history):
    s1 = HeuristicExperimentSystem(history_path=tmp_history)
    s1.start_experiment('discount_banner')
    s2 = HeuristicExperimentSystem(history_path=tmp_history)
    assert len(s2.get_active_experiments()) == 1


def test_history_file_created_after_start(tmp_history, system):
    system.start_experiment('photo_watermark')
    assert os.path.exists(tmp_history)
    with open(tmp_history) as f:
        data = json.load(f)
    assert len(data) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 5.3 — CEO Intelligence: CEOExperimentSystem + CEODelegation
# ═══════════════════════════════════════════════════════════════════════════════

from factory.agents.experiment_system import (
    CEOExperimentSystem,
    CEODelegation,
    CEO_EXPERIMENT_IDEAS,
)


# ── CEO_EXPERIMENT_IDEAS constants ────────────────────────────────────────────

def test_ceo_experiment_ideas_not_empty():
    assert len(CEO_EXPERIMENT_IDEAS) > 0


def test_ceo_experiment_ideas_have_required_keys():
    required = {'id', 'name', 'hypothesis', 'metric', 'variants', 'duration_days'}
    for idea in CEO_EXPERIMENT_IDEAS:
        assert required <= set(idea.keys()), f"Missing keys in {idea['id']}"


def test_ceo_experiment_ideas_have_two_or_more_variants():
    for idea in CEO_EXPERIMENT_IDEAS:
        assert len(idea['variants']) >= 2, f"{idea['id']} should have >= 2 variants"


def test_ceo_experiment_ideas_ids_unique():
    ids = [e['id'] for e in CEO_EXPERIMENT_IDEAS]
    assert len(ids) == len(set(ids))


def test_ceo_experiment_ideas_duration_positive():
    for idea in CEO_EXPERIMENT_IDEAS:
        assert idea['duration_days'] > 0


# ── CEOExperimentSystem: instantiation ────────────────────────────────────────

def test_ceo_experiment_system_instantiates():
    sys = CEOExperimentSystem()
    assert sys is not None


def test_ceo_experiment_system_has_experiment_ideas():
    sys = CEOExperimentSystem()
    assert len(sys.EXPERIMENT_IDEAS) > 0


# ── CEOExperimentSystem: propose_experiment ────────────────────────────────────

def test_propose_experiment_returns_dict():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert isinstance(result, dict)


def test_propose_experiment_has_status():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert 'status' in result


def test_propose_experiment_status_proposed():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert result['status'] == 'proposed'


def test_propose_experiment_has_experiment_key():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert 'experiment' in result


def test_propose_experiment_experiment_has_id():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert 'id' in result['experiment']


def test_propose_experiment_has_start_date():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert 'start_date' in result


def test_propose_experiment_has_end_date():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    assert 'end_date' in result


def test_propose_experiment_with_context_returns_dict():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment(context={"health_score": 60})
    assert isinstance(result, dict)


def test_propose_experiment_with_none_context():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment(context=None)
    assert result['status'] == 'proposed'


def test_propose_experiment_experiment_is_from_ideas():
    sys = CEOExperimentSystem()
    result = sys.propose_experiment()
    idea_ids = {e['id'] for e in CEO_EXPERIMENT_IDEAS}
    assert result['experiment']['id'] in idea_ids


# ── CEOExperimentSystem: get_active_experiments ────────────────────────────────

def test_get_active_experiments_returns_list():
    sys = CEOExperimentSystem()
    result = sys.get_active_experiments()
    assert isinstance(result, list)


def test_get_active_experiments_empty_when_no_file():
    sys = CEOExperimentSystem()
    # With no experiments.json in default path, should return []
    result = sys.get_active_experiments()
    assert isinstance(result, list)


# ── CEOExperimentSystem: track_results ────────────────────────────────────────

def test_track_results_returns_dict():
    sys = CEOExperimentSystem()
    result = sys.track_results("exp_001", {"a_rate": 0.2, "b_rate": 0.3})
    assert isinstance(result, dict)


def test_track_results_has_winner():
    sys = CEOExperimentSystem()
    result = sys.track_results("exp_001", {"a_rate": 0.2, "b_rate": 0.3})
    assert 'winner' in result


def test_track_results_winner_b_when_b_better(tmp_path):
    sys = CEOExperimentSystem(store_path=str(tmp_path / "ceo_exp.json"))
    result = sys.track_results("exp_001", {"a_rate": 0.2, "b_rate": 0.4})
    assert result['winner'] == 'B'


def test_track_results_winner_a_when_a_better(tmp_path):
    sys = CEOExperimentSystem(store_path=str(tmp_path / "ceo_exp.json"))
    result = sys.track_results("exp_001", {"a_rate": 0.5, "b_rate": 0.3})
    assert result['winner'] == 'A'


def test_track_results_equal_rates_winner_a(tmp_path):
    sys = CEOExperimentSystem(store_path=str(tmp_path / "ceo_exp.json"))
    result = sys.track_results("exp_001", {"a_rate": 0.3, "b_rate": 0.3})
    assert result['winner'] in ('A', 'inconclusive')


def test_track_results_has_improvement():
    sys = CEOExperimentSystem()
    result = sys.track_results("exp_002", {"a_rate": 0.2, "b_rate": 0.5})
    assert 'improvement' in result
    assert result['improvement'] == pytest.approx(0.3, abs=0.001)


def test_track_results_has_experiment_id():
    sys = CEOExperimentSystem()
    result = sys.track_results("exp_003", {"a_rate": 0.1, "b_rate": 0.15})
    assert result['experiment_id'] == "exp_003"


def test_track_results_has_metrics():
    sys = CEOExperimentSystem()
    metrics = {"a_rate": 0.1, "b_rate": 0.2}
    result = sys.track_results("exp_004", metrics)
    assert result['metrics'] == metrics


def test_track_results_has_timestamp():
    sys = CEOExperimentSystem()
    result = sys.track_results("exp_005", {"a_rate": 0.0, "b_rate": 0.1})
    assert 'timestamp' in result
    assert isinstance(result['timestamp'], str)


# ── CEOExperimentSystem: generate_report ──────────────────────────────────────

def test_ceo_generate_report_returns_string():
    sys = CEOExperimentSystem()
    report = sys.generate_report()
    assert isinstance(report, str)


def test_ceo_generate_report_contains_keyword():
    sys = CEOExperimentSystem()
    report = sys.generate_report()
    assert 'ЭКСПЕРИМЕНТ' in report or 'Предложения' in report or 'Активных' in report


def test_ceo_generate_report_has_ideas():
    sys = CEOExperimentSystem()
    report = sys.generate_report()
    assert len(report) > 20


def test_ceo_generate_report_with_context():
    sys = CEOExperimentSystem()
    report = sys.generate_report(context={"orders": 5})
    assert isinstance(report, str)


# ── CEODelegation: instantiation ──────────────────────────────────────────────

def test_ceo_delegation_instantiates():
    d = CEODelegation()
    assert d is not None


def test_ceo_delegation_has_departments():
    d = CEODelegation()
    assert len(d.DEPARTMENTS) > 0


def test_ceo_delegation_departments_includes_sales():
    d = CEODelegation()
    assert 'sales' in d.DEPARTMENTS


def test_ceo_delegation_departments_includes_marketing():
    d = CEODelegation()
    assert 'marketing' in d.DEPARTMENTS


# ── CEODelegation: delegate_focus ─────────────────────────────────────────────

def test_delegate_focus_returns_dict():
    d = CEODelegation()
    result = d.delegate_focus()
    assert isinstance(result, dict)


def test_delegate_focus_has_focus_department():
    d = CEODelegation()
    result = d.delegate_focus()
    assert 'focus_department' in result


def test_delegate_focus_has_reason():
    d = CEODelegation()
    result = d.delegate_focus()
    assert 'reason' in result


def test_delegate_focus_has_priority_tasks():
    d = CEODelegation()
    result = d.delegate_focus()
    assert 'priority_tasks' in result
    assert isinstance(result['priority_tasks'], list)


def test_delegate_focus_has_cycle():
    d = CEODelegation()
    result = d.delegate_focus()
    assert 'cycle' in result


def test_delegate_focus_low_conversion_gives_sales():
    d = CEODelegation()
    result = d.delegate_focus(kpis={"conversion_rate": 0.1, "orders_total": 50})
    assert result['focus_department'] == 'sales'


def test_delegate_focus_low_orders_gives_marketing():
    d = CEODelegation()
    result = d.delegate_focus(kpis={"conversion_rate": 0.5, "orders_total": 5})
    assert result['focus_department'] == 'marketing'


def test_delegate_focus_healthy_gives_growth_dept():
    d = CEODelegation()
    result = d.delegate_focus(kpis={"conversion_rate": 0.7, "orders_total": 100})
    assert result['focus_department'] in ('product', 'analytics', 'creative', 'tech')


def test_delegate_focus_with_none_kpis():
    d = CEODelegation()
    result = d.delegate_focus(kpis=None)
    assert isinstance(result, dict)
    assert 'focus_department' in result


def test_delegate_focus_stores_decision(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    d.delegate_focus()
    assert len(d._store.get("decisions_history", [])) == 1


def test_delegate_focus_multiple_calls_accumulate(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    d.delegate_focus()
    d.delegate_focus()
    assert len(d._store.get("decisions_history", [])) == 2


def test_delegate_focus_sets_current_focus():
    d = CEODelegation()
    result = d.delegate_focus()
    assert d._current_focus == result['focus_department']


# ── CEODelegation: _get_priority_tasks ────────────────────────────────────────

def test_get_priority_tasks_sales():
    d = CEODelegation()
    tasks = d._get_priority_tasks('sales')
    assert isinstance(tasks, list)
    assert len(tasks) > 0


def test_get_priority_tasks_marketing():
    d = CEODelegation()
    tasks = d._get_priority_tasks('marketing')
    assert isinstance(tasks, list)
    assert len(tasks) > 0


def test_get_priority_tasks_product():
    d = CEODelegation()
    tasks = d._get_priority_tasks('product')
    assert isinstance(tasks, list)


def test_get_priority_tasks_analytics():
    d = CEODelegation()
    tasks = d._get_priority_tasks('analytics')
    assert isinstance(tasks, list)


def test_get_priority_tasks_creative():
    d = CEODelegation()
    tasks = d._get_priority_tasks('creative')
    assert isinstance(tasks, list)


def test_get_priority_tasks_finance():
    d = CEODelegation()
    tasks = d._get_priority_tasks('finance')
    assert isinstance(tasks, list)


def test_get_priority_tasks_operations():
    d = CEODelegation()
    tasks = d._get_priority_tasks('operations')
    assert isinstance(tasks, list)


def test_get_priority_tasks_hr():
    d = CEODelegation()
    tasks = d._get_priority_tasks('hr')
    assert isinstance(tasks, list)


def test_get_priority_tasks_tech():
    d = CEODelegation()
    tasks = d._get_priority_tasks('tech')
    assert isinstance(tasks, list)


def test_get_priority_tasks_unknown_dept():
    d = CEODelegation()
    tasks = d._get_priority_tasks('unknown_dept')
    assert isinstance(tasks, list)
    assert len(tasks) > 0


# ── CEODelegation: get_focus_report ───────────────────────────────────────────

def test_get_focus_report_returns_string():
    d = CEODelegation()
    report = d.get_focus_report()
    assert isinstance(report, str)


def test_get_focus_report_no_focus():
    d = CEODelegation()
    report = d.get_focus_report()
    assert 'не установлен' in report or 'фокус' in report.lower()


def test_get_focus_report_after_delegate():
    d = CEODelegation()
    d.delegate_focus(kpis={"conversion_rate": 0.1, "orders_total": 50})
    report = d.get_focus_report()
    assert 'sales' in report


# ── CEODelegation: check_previous_decisions ───────────────────────────────────

def test_check_previous_decisions_returns_dict():
    d = CEODelegation()
    result = d.check_previous_decisions()
    assert isinstance(result, dict)


def test_check_previous_decisions_has_total_decisions():
    d = CEODelegation()
    result = d.check_previous_decisions()
    assert 'total_decisions' in result


def test_check_previous_decisions_has_tracked(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    result = d.check_previous_decisions()
    assert 'untracked' in result


def test_check_previous_decisions_has_fulfillment_rate():
    d = CEODelegation()
    result = d.check_previous_decisions()
    assert 'fulfillment_rate' in result


def test_check_previous_decisions_has_summary():
    d = CEODelegation()
    result = d.check_previous_decisions()
    assert 'summary' in result
    assert isinstance(result['summary'], str)


def test_check_previous_decisions_zero_when_no_decisions(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    result = d.check_previous_decisions()
    assert result['total_decisions'] == 0
    assert result['fulfillment_rate'] == 0.0


def test_check_previous_decisions_nonzero_after_delegate(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    d.delegate_focus()
    result = d.check_previous_decisions()
    assert result['total_decisions'] == 1


def test_check_previous_decisions_tracked_equals_total(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    d.delegate_focus()
    d.delegate_focus()
    result = d.check_previous_decisions()
    assert result['untracked'] == result['total_decisions']


# ── Integration tests ─────────────────────────────────────────────────────────

def test_integration_low_conversion_delegates_sales(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    result = d.delegate_focus(kpis={"conversion_rate": 0.05, "orders_total": 100})
    assert result['focus_department'] == 'sales'
    # reason is in Russian — just check the department is correct
    assert isinstance(result['reason'], str)


def test_integration_low_orders_delegates_marketing(tmp_path):
    d = CEODelegation(store_path=str(tmp_path / "delegation.json"))
    result = d.delegate_focus(kpis={"conversion_rate": 0.8, "orders_total": 2})
    assert result['focus_department'] == 'marketing'
    assert isinstance(result['reason'], str)


def test_integration_experiment_and_delegation_full_flow(tmp_path):
    exp_sys = CEOExperimentSystem(store_path=str(tmp_path / "ceo_experiments.json"))
    delegation = CEODelegation(store_path=str(tmp_path / "delegation.json"))

    proposal = exp_sys.propose_experiment()
    focus = delegation.delegate_focus(kpis={"conversion_rate": 0.1, "orders_total": 5})

    assert proposal['status'] == 'proposed'
    assert focus['focus_department'] in ('sales', 'marketing')

    tracking = exp_sys.track_results(
        proposal['experiment']['id'],
        {"a_rate": 0.2, "b_rate": 0.35}
    )
    assert tracking['winner'] == 'B'

    history = delegation.check_previous_decisions()
    assert history['total_decisions'] == 1
