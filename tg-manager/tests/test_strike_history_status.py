"""История Strike показывает честный статус цели и настойчивую эскалацию.

Пользователь запускает настойчивую эскалацию, но в истории не видел её состояния, а
статус цели терялся в bool(): «подтверждённо жива» и «не проверяли» выглядели
одинаково. Добавлено:
  • verified_status в завершённых: 'down' | 'up' | 'unknown' (снята/активна/не
    проверено) вместо плоского bool;
  • флаг escalation у отложенных заходов (pending + scheduled_for в будущем +
    persist в params) — «под эскалацией», а не просто «в очереди».
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _history_body() -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def strike_history\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "strike_history не найден"
    return m.group(1)


def test_done_exposes_tristate_verified_status():
    body = _history_body()
    assert '"verified_status"' in body, "нужен трёхзначный статус цели"
    # именно down/up/unknown, а не только bool
    assert '"down"' in body and '"up"' in body and '"unknown"' in body
    assert 'r["verified_down"] is True' in body and 'r["verified_down"] is False' in body, (
        "различаем подтверждённо снята / подтверждённо жива / не проверено"
    )


def test_active_flags_scheduled_escalation():
    body = _history_body()
    # отложенный заход эскалации: persist + scheduled_for в будущем
    assert '"escalation"' in body, "отложенный заход эскалации должен помечаться"
    assert 'params.get("persist")' in body or '_params.get("persist")' in body
    assert "scheduled_for" in body, "нужен scheduled_for для отличия заплана от очереди"


def test_active_query_selects_params_and_schedule():
    body = _history_body()
    # без этих полей нельзя отличить эскалацию от обычной очереди
    assert "scheduled_for, params" in body or "scheduled_for" in body and "params" in body


def test_ui_renders_target_status_badges():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "verified_status" in html, "UI должен читать трёхзначный статус"
    assert "ещё активна" in html and "не проверено" in html, (
        "UI должен честно показывать активную/непроверенную цель, не только «снята»"
    )
    assert "под эскалацией" in html, "UI должен показывать заходы под эскалацией"
