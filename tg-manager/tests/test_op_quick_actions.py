"""Быстрые действия по итогу операции — вместо советов словами.

Разрыв с живого отчёта инвайта: сводка объясняла проблему и называла лечение
ТЕКСТОМ, а кнопки рядом не было.

  «⚠️ Без прокси (риск блокировок): 28 — назначьте прокси для изоляции»
  «🚫 3 аккаунтов без прав админа выведены из круга — проверьте автовыдачу»
  «🛑 Флот перегрет — дайте им отдохнуть и повторите позже»

Плюс статичная карта «тип операции → одна кнопка» покрывала 4 типа из ~70 и на
исход не смотрела вовсе.
"""
from __future__ import annotations

import pathlib
import re

from services import op_quick_actions as QA

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
_SCREENS = "\n".join(
    p.read_text(encoding="utf-8")
    for p in sorted((_ROOT / "mini_app" / "screens").glob("*.js"))
)
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_OPW = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _ids(*a, **kw):
    return [x["id"] for x in QA.suggest(*a, **kw)]


# ── Проблемы исхода превращаются в действия ────────────────────────────────

def test_accounts_without_proxy_get_an_action():
    acts = QA.suggest("mass_invite", "done", {"no_proxy": 28}, op_id=5)
    a = next(x for x in acts if x["id"] == "assign_proxy")
    assert "28" in a["label"] and a["fn"] == "openProxies"
    assert a["reason"]


def test_accounts_dropped_without_rights_get_an_action():
    acts = QA.suggest("mass_invite", "done", {"no_rights_retired": 3}, op_id=5)
    assert any(x["id"] == "check_rights" for x in acts)


def test_flood_storm_points_at_fleet_health():
    assert "fleet_health" in _ids("mass_invite", "done", {"flood_storm": True}, op_id=5)


def test_leftover_targets_offer_finishing_the_job():
    acts = QA.suggest("mass_invite", "done", {"left": 177}, op_id=42)
    a = next(x for x in acts if x["id"] == "retry_left")
    assert a["arg"] == 42 and "177" in a["label"]


def test_no_leftover_action_when_continuation_already_scheduled():
    """Система сама доработает остаток — звать человека незачем."""
    assert "retry_left" not in _ids(
        "mass_invite", "done", {"left": 177, "next_op_id": 99}, op_id=42)


def test_dead_sessions_send_to_accounts_not_to_retry():
    acts = QA.suggest("mass_invite", "done", {"all_failed_connect": True}, op_id=5)
    a = next(x for x in acts if x["id"] == "fix_accounts")
    assert a["fn"] == "goTab" and a["arg"] == "accounts"


def test_failed_operation_offers_retry_with_its_id():
    a = next(x for x in QA.suggest("mass_publish", "failed", {}, op_id=7)
             if x["id"] == "retry")
    assert a["arg"] == 7


# ── Порядок и полнота ──────────────────────────────────────────────────────

def test_fleet_problems_come_before_the_generic_next_step():
    """Иначе полезное действие тонет под обычным «следующим шагом»."""
    ids = _ids("mass_invite", "done", {"no_proxy": 28}, op_id=5)
    assert ids.index("assign_proxy") < ids.index("next_mass_invite")


def test_generic_next_step_covers_many_more_op_types_than_before():
    """Раньше статичная карта знала 4 типа из ~70."""
    assert len(QA._AFTER_DONE) >= 12


def test_scan_leads_to_connecting_found_bots():
    assert "next_scan_owned_bots" in _ids("scan_owned_bots", "done", {})


def test_failed_operation_gets_no_generic_next_step():
    """После провала «следующий шаг» — ложь: шага не было."""
    assert "next_mass_invite" not in _ids("mass_invite", "failed", {}, op_id=1)


def test_no_duplicate_actions():
    acts = QA.suggest("mass_invite", "done",
                      {"no_proxy": 5, "no_rights_retired": 2, "left": 10,
                       "flood_storm": True}, op_id=1)
    assert len({a["id"] for a in acts}) == len(acts)


def test_clean_result_yields_only_the_next_step():
    assert _ids("mass_invite", "done", {"ok": 10, "left": 0}, op_id=1) == \
        ["next_mass_invite"]


def test_garbage_input_does_not_crash():
    assert QA.suggest(None, None, None) == []
    assert QA.suggest("mass_invite", "done", {"no_proxy": "x"}, op_id=1) is not None


# ── Ни одной мёртвой кнопки ────────────────────────────────────────────────

def test_every_suggested_function_exists_in_the_mini_app():
    """Действие, чья функция не существует, — мёртвая кнопка."""
    fns = {v[1] for v in QA._AFTER_DONE.values()}
    fns |= {"openProxies", "openChannels", "openHealth", "retryOp", "goTab"}
    src = _UI + _SCREENS
    missing = [f for f in sorted(fns)
               if not re.search(rf"(async\s+)?function\s+{re.escape(f)}\s*\(", src)]
    assert not missing, f"нет таких функций в мини-аппе: {missing}"


# ── Проводка ───────────────────────────────────────────────────────────────

def test_status_endpoint_returns_quick_actions():
    assert "op_quick_actions" in _API
    assert '"quick_actions"' in _API
    assert "result AS _result" in _API, "нужен структурный результат, а не текст сводки"


def test_invite_returns_problem_counters_structurally():
    """Кнопки строятся по данным, а не по разбору текста сводки — он меняется."""
    assert '"no_proxy": _no_proxy_n' in _OPW
    assert '"no_rights_retired": _no_rights_retired' in _OPW


def test_ui_renders_quick_actions_and_skips_missing_functions():
    assert "_qaButtons" in _UI
    assert "typeof window[a.fn] === 'function'" in _UI or \
           "typeof window[a.fn] !== 'function'" in _UI
    assert "Что сделать дальше" in _UI
