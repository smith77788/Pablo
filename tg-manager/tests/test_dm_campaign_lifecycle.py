"""Жизненный цикл DM-кампании: пауза, перезапуск, честный срок.

Что было сломано в экране кампаний:
  - кнопка «Возобновить» для paused была, а поставить идущую кампанию на ПАУЗУ
    было нечем: остановить рассылку можно было только отменой операции в другом
    экране, хотя движок паузу поддерживает (проверяет статус перед каждым
    получателем);
  - кампания со статусом failed становилась тупиком: кнопок нет, только
    удалить — хотя бэкенд разрешает повторный запуск из любого статуса, кроме
    running (а журнал отправок защищает от повторов);
  - на экране было только «0/5000 отправлено» без срока: при паузе 45–110 с на
    сообщение это недели, и пользователь узнавал об этом уже по факту.
"""
from __future__ import annotations

import pathlib

from services.dm_engine import (
    _DELAYS_BY_TARGET_TYPE,
    _PACE_MULTIPLIERS,
    estimate_duration_seconds,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


# ── Оценка срока ──────────────────────────────────────────────────────────────

def test_eta_scales_with_audience():
    """Срок линеен по числу получателей (до округления в самом конце)."""
    ten = estimate_duration_seconds("crm", "normal", 10)
    hundred = estimate_duration_seconds("crm", "normal", 100)
    assert hundred == 10 * ten
    assert estimate_duration_seconds("crm", "normal", 1) < ten


def test_eta_respects_pace():
    normal = estimate_duration_seconds("crm", "normal", 100)
    assert estimate_duration_seconds("crm", "slow", 100) > normal
    assert estimate_duration_seconds("crm", "fast", 100) < normal


def test_eta_uses_target_type_delays():
    """Незнакомая аудитория медленнее своих подписчиков — срок обязан это отражать."""
    assert (estimate_duration_seconds("import_list", "normal", 100)
            > estimate_duration_seconds("bot_users", "normal", 100))


def test_eta_matches_engine_delay_table():
    """Срок обязан считаться по ТОЙ ЖЕ таблице, что выдерживает движок."""
    dmin, dmax = _DELAYS_BY_TARGET_TYPE["crm"]
    assert estimate_duration_seconds("crm", "normal", 4) == int(4 * (dmin + dmax) / 2)


def test_eta_auto_pace_falls_back_to_normal():
    """Реальный множитель 'auto' известен только движку на старте."""
    assert (estimate_duration_seconds("crm", "auto", 50)
            == estimate_duration_seconds("crm", "normal", 50))


def test_eta_handles_garbage_without_crashing():
    for bad in (0, -5, None, "х", 1.7):
        assert estimate_duration_seconds("crm", "normal", bad) >= 0


def test_eta_unknown_target_type_uses_default_not_zero():
    assert estimate_duration_seconds("невиданный_тип", "normal", 10) > 0


def test_pace_multipliers_shared_with_send_loop():
    """Один источник правды: UI не должен обещать темп, отличный от движка."""
    src = (_ROOT / "services" / "dm_engine.py").read_text(encoding="utf-8")
    assert '{"slow": 2.0, "normal": 1.0, "fast": 0.5}.get(_pace)' not in src, (
        "множители темпа продублированы в send-цикле — расчёт срока разъедется"
    )
    assert set(_PACE_MULTIPLIERS) == {"slow", "normal", "fast"}


# ── Пауза ─────────────────────────────────────────────────────────────────────

def test_pause_endpoint_exists_and_is_routed():
    assert "async def dm_campaign_pause" in _API
    assert '"/api/miniapp/dm_campaign/{campaign_id}/pause"' in _API


def test_pause_only_for_running_campaign():
    """Пауза черновика или завершённой кампании — бессмыслица, ждём отказ."""
    start = _API.index("async def dm_campaign_pause")
    body = _API[start:start + 2000]
    assert 'row["status"] != "running"' in body
    assert "409" in body


def test_pause_is_owner_scoped():
    """Чужую кампанию остановить нельзя."""
    start = _API.index("async def dm_campaign_pause")
    body = _API[start:start + 2000]
    assert "owner_id=$2" in body


def test_ui_has_pause_button_for_running():
    assert "pauseDm(" in _UI, "у идущей кампании должна быть кнопка паузы"
    assert "c.status==='running'?" in _UI


def test_ui_has_retry_button_for_failed():
    assert "c.status==='failed'?" in _UI, "упавшая кампания не должна быть тупиком"


def test_ui_shows_eta_and_progress():
    assert "eta_seconds" in _UI and "humanDur(" in _UI
    assert "function humanDur" in _UI, "форматтер срока обязан существовать"


def test_list_endpoint_returns_eta():
    assert "estimate_duration_seconds" in _API and '"eta_seconds"' in _API.replace("'", '"')


# ── Честный итог операции ─────────────────────────────────────────────────────

def test_op_summary_distinguishes_paused_from_finished():
    src = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    start = src.index("async def _exec_dm_campaign")
    body = src[start:start + 4000]
    assert "на паузе" in body, (
        "поставленная на паузу кампания не должна выглядеть завершённой в панели операций"
    )


# ── Удаление во время рассылки ────────────────────────────────────────────────

def test_deleted_campaign_stops_the_send_loop():
    """Дефект намерения: прежняя проверка `if current and ...` не отличала
    «строки нет» от «не на паузе». Удалив идущую кампанию, пользователь не
    останавливал её — цикл продолжал слать ЛС РЕАЛЬНЫМ ЛЮДЯМ по удалённой
    кампании, пока не падал на FK-вставке в журнал отправок.
    """
    src = (_ROOT / "services" / "dm_engine.py").read_text(encoding="utf-8")
    start = src.index("async def run_campaign")
    body = src[start:]
    assert "if current is None:" in body, (
        "исчезнувшая строка кампании обязана останавливать рассылку"
    )
    assert 'if current and current["status"] == "paused"' not in body, (
        "старая проверка пропускала удаление кампании"
    )


def test_delete_warns_about_consequences():
    assert "deleteDm(id, status)" in _UI.replace("async function deleteDm(id, status)",
                                                 "deleteDm(id, status)")
    assert "ИДУЩУЮ кампанию" in _UI, "удаление идущей кампании требует явного предупреждения"
    assert "журнал отправок" in _UI, (
        "удаление стирает журнал — значит и отчёт, и защиту от повторной отправки"
    )
