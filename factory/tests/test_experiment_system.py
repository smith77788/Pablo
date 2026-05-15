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
