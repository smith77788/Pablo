"""Отчёт по DM-кампании: собранные данные должны доходить до пользователя.

Разрыв, который это закрывает: dm_campaign_log писался с самого начала (статус
и причина по каждому получателю, какой аккаунт отправлял), но не читался НИ
ОДНИМ экраном и ни одним эндпоинтом. Пользователь видел «✅ N ❌ M» и не мог
узнать, кому не дошло, почему и какой аккаунт сыпет ошибками — данные
собирались и выбрасывались. Это разрыв в звене AUDIT → USER FEEDBACK.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _report_body() -> str:
    start = _API.index("async def dm_campaign_report")
    return _API[start:start + 4000]


def test_report_endpoint_exists_and_is_routed():
    assert "async def dm_campaign_report" in _API
    assert '"/api/miniapp/dm_campaign/{campaign_id}/report"' in _API


def test_report_is_owner_scoped_against_idor():
    """Чужой отчёт — это чужая аудитория. Скоуп владельца обязателен."""
    body = _report_body()
    assert "owner_id=$2" in body
    assert "404" in body


def test_report_reads_the_previously_unused_log():
    body = _report_body()
    assert body.count("dm_campaign_log") >= 4, (
        "отчёт обязан читать журнал отправок, ради которого он и писался"
    )


def test_report_breaks_down_by_status():
    body = _report_body()
    assert "GROUP BY status" in body


def test_report_breaks_down_by_account():
    """Аккаунт, который сыпет ошибками, тонет в общем счётчике — нужна разбивка."""
    body = _report_body()
    assert "l.account_id" in body and "tg_accounts" in body
    assert "FILTER (WHERE l.status='sent')" in body


def test_report_surfaces_top_error_reasons():
    body = _report_body()
    assert "error_msg" in body and "ORDER BY n DESC" in body


def test_report_recent_rows_are_bounded():
    """Кампания на 20k получателей не должна выгружаться в ответ целиком."""
    body = _report_body()
    assert "LIMIT 100" in body and "LIMIT 50" in body


def test_report_serialises_timestamps():
    body = _report_body()
    assert "isoformat()" in body, "sent_at обязан сериализоваться, иначе ответ не отдастся"


def test_report_uses_failopen_helpers():
    """Отчёт — диагностика: частичный результат лучше 500-й ошибки."""
    body = _report_body()
    assert "_safe_fetch" in body


# ── Интерфейс ─────────────────────────────────────────────────────────────────

def test_ui_has_report_screen_and_opener():
    assert 'id="s-dmreport"' in _UI, "нужен экран отчёта"
    assert "function openDmReport" in _UI
    assert "openDmReport(" in _UI.split("function openDmReport")[0], (
        "на отчёт должен быть переход из списка кампаний"
    )


def test_ui_report_targets_the_report_endpoint():
    assert "'/api/miniapp/dm_campaign/'+id+'/report'" in _UI


def test_ui_translates_log_statuses_for_humans():
    """Сырые 'peer_flood'/'skip' пользователю ничего не говорят."""
    assert "DM_LOG_STATUS" in _UI
    for key in ("sent:", "blocked:", "skip:", "peer_flood:"):
        assert key in _UI, key


def test_ui_handles_empty_report():
    """Кампания без единой отправки не должна выглядеть поломкой."""
    seg = _UI[_UI.index("function openDmReport"):]
    seg = seg[:4000]
    assert "Отправок ещё не было" in seg
