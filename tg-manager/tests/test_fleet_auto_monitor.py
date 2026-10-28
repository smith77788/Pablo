"""Авто-мониторинг флота: периодические фоновые проверки через рекуррентные op.

Включил «проверяй флот каждые N часов» — и read-only проверки (здоровье,
ограничения, гео-бан) ставятся рекуррентно через уже проверенную инфру
op_worker (repeat_interval_min + _RECURRING_OK_OPS), находки идут в пульс.
Сторожим: типы разрешены к рекуррентности И зарегистрированы в шине; ручки
ставят рекуррентную операцию и снимают прежнюю; UI умеет вкл/выкл.
"""
from __future__ import annotations

from services.mini_app_monitor import _MONITOR_OPS, _MIN_INTERVAL_MIN, _MAX_INTERVAL_MIN
from services.op_worker import _RECURRING_OK_OPS
from services.operation_bus import OP_REGISTRY
from tests.miniapp_source import api_function, js_function


def test_monitor_ops_are_recurring_and_registered():
    # Каждый вид мониторинга → реальный op_type, который можно зацикливать И
    # который знает шина (иначе submit/рекуррентность не сработают).
    for kind, op_type in _MONITOR_OPS.items():
        assert op_type in _RECURRING_OK_OPS, f"{op_type} не разрешён к рекуррентности"
        assert op_type in OP_REGISTRY, f"{op_type} не зарегистрирован в шине"


def test_monitor_ops_are_read_only_checks():
    # Зацикливаем только ПРОВЕРКИ (check_*), не действия по аккаунтам.
    for op_type in _MONITOR_OPS.values():
        assert op_type.startswith("check_"), f"{op_type} — не read-only проверка"


def test_interval_bounds_sane():
    assert _MIN_INTERVAL_MIN >= 60          # не чаще раза в час
    assert _MAX_INTERVAL_MIN <= 60 * 24 * 31


def test_toggle_submits_recurring_and_cancels_previous():
    body = api_function("monitor_toggle")
    assert '"repeat_interval_min": interval' in body
    assert "_obus.submit(pool, uid, op_type, params" in body
    # дедуп/остановка: прежний круг снимается через единую дверь отмены
    assert "_obus.cancel(pool, int(existing[\"id\"]), uid" in body
    # интервал зажат в границы
    assert "max(_MIN_INTERVAL_MIN, min(interval, _MAX_INTERVAL_MIN))" in body


def test_status_route_reports_each_kind():
    body = api_function("monitor_status")
    assert "_active_recurring(uid, op_type)" in body
    assert '"enabled": bool(row)' in body


def test_ui_has_monitor_controls():
    render = js_function("_shMonRender")
    assert "/api/miniapp/monitor" not in render  # запрос делает _shLoadBody, не render
    assert "shMonToggle(" in render
    toggle = js_function("shMonToggle")
    assert "/api/miniapp/monitor/toggle" in toggle
    load = js_function("_shLoadBody")
    assert "/api/miniapp/monitor/status" in load
