"""Перезапуск упавших операций: «упавшая» — это недоведённая, а не статус.

Разрыв с живого прогона. Массовый инвайт остановился на 203 целях из 380 со
177 ошибками и завершился со статусом `done` — исполнитель отработал и вернул
сводку. Кнопка повтора показывалась ТОЛЬКО для `failed`, поэтому перезапустить
такую операцию было нечем: пользователь видел «✅ done», недостигнутую цель и ни
одного способа продолжить. Массового перезапуска не было вовсе.
"""
from __future__ import annotations

import ast
import pathlib

from services import operation_retry as R

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Главный регресс ────────────────────────────────────────────────────────

def test_done_but_goal_not_reached_is_retryable():
    """Ровно случай из отчёта: 203 из 380, статус done."""
    ok, why = R.can_retry("done", done_items=203, total_items=380, err_count=177)
    assert ok is True
    assert "203" in why and "380" in why


def test_done_with_errors_is_retryable_even_when_counted_complete():
    ok, why = R.can_retry("done", done_items=380, total_items=380, err_count=177)
    assert ok is True and "ошибк" in why


def test_fully_successful_operation_is_not_retryable():
    """Иначе кнопка предлагала бы повторить то, что уже сделано — дубли."""
    ok, why = R.can_retry("done", done_items=380, total_items=380, err_count=0)
    assert ok is False and "полностью" in why


# ── Прочие статусы ─────────────────────────────────────────────────────────

def test_failed_is_retryable():
    assert R.can_retry("failed")[0] is True


def test_cancelled_is_retryable():
    assert R.can_retry("cancelled")[0] is True


def test_in_flight_operations_are_not_retryable():
    for st in ("pending", "running", "paused", "scheduled"):
        ok, why = R.can_retry(st, 1, 10, 0)
        assert ok is False, st
        assert "работе" in why


def test_unknown_status_does_not_lock_the_work():
    ok, _why = R.can_retry("weird_state")
    assert ok is True


def test_garbage_counters_do_not_crash():
    assert R.can_retry("done", None, None, None)[0] is False
    assert R.can_retry("done", "x", "y", "z")[0] is False


def test_status_is_case_and_space_insensitive():
    assert R.can_retry("  FAILED ")[0] is True


# ── Массовый отбор ─────────────────────────────────────────────────────────

def test_pick_retryable_filters_and_explains():
    rows = [
        {"id": 1, "status": "done", "done_items": 203, "total_items": 380, "err_cnt": 177},
        {"id": 2, "status": "done", "done_items": 10, "total_items": 10, "err_cnt": 0},
        {"id": 3, "status": "failed", "done_items": 0, "total_items": 5, "err_cnt": 1},
        {"id": 4, "status": "running", "done_items": 1, "total_items": 9, "err_cnt": 0},
    ]
    got = R.pick_retryable(rows)
    assert [g["id"] for g in got] == [1, 3]
    assert all(g["reason"] for g in got)


def test_pick_retryable_survives_empty_and_broken_input():
    assert R.pick_retryable([]) == []
    assert R.pick_retryable(None) == []


# ── Проводка ───────────────────────────────────────────────────────────────

def test_single_retry_uses_the_decision_not_a_bare_status_check():
    src = _func_src(_API, "retry_operation")
    assert "operation_retry" in src and "can_retry" in src
    # Старая жёсткая проверка «только failed» должна исчезнуть.
    assert 'row["status"] != "failed"' not in src
    # Счётчики обязаны доезжать до решения.
    assert "done_items" in src and "err_cnt" in src


def test_bulk_retry_endpoint_exists_and_is_owner_scoped():
    src = _func_src(_API, "operations_retry_failed")
    assert "pick_retryable" in src
    assert "owner_id=$1" in src
    assert "dedup_window_sec=0" in src, (
        "повтор — намеренно та же операция, окно идемпотентности приняло бы "
        "его за двойной тап")
    assert 'app.router.add_post("/api/miniapp/operations/retry_failed"' in _API


def test_bulk_retry_is_capped():
    """Одно нажатие не должно заваливать очередь сотнями операций."""
    src = _func_src(_API, "operations_retry_failed")
    assert "[:25]" in src


def test_bulk_retry_reports_skipped_instead_of_failing_silently():
    src = _func_src(_API, "operations_retry_failed")
    assert "skipped" in src and "PermissionError" in src


def test_ui_shows_retry_for_incomplete_done_and_offers_bulk():
    assert "function opCanRetry" in _UI
    assert "opCanRetry(o)" in _UI
    assert "retryAllFailed" in _UI
    # Старое условие «только failed / mass_publish» в списке операций ушло.
    assert "o.status==='failed'||(o.op_type==='mass_publish'" not in _UI
