"""Тихие часы: не писать живым людям среди ночи.

Разрыв, который это закрывает: кампания на тысячи получателей при паузе
45–110 с на сообщение идёт СУТКАМИ и неизбежно попадает на ночь. Движок слал
ЛС в 4 утра — это и низкий отклик, и жалобы на спам, от которых горят
аккаунты. Ночного режима у рассылки не было вовсе, хотя geo_tempo умеет
определять локальную ночь и им уже пользуется прогрев чатов.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

from services.dm_engine import _QUIET_MAX_WAIT_S, accounts_awake

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 01:00 UTC = 04:00 МСК — ночь для RU, день для US.
_NIGHT_MSK = dt.datetime(2026, 9, 4, 1, 0, tzinfo=dt.timezone.utc)
# 11:00 UTC — день и в России (14:00), и в США (07:00).
_MIDDAY_BOTH = dt.datetime(2026, 9, 4, 11, 0, tzinfo=dt.timezone.utc)


def test_night_accounts_are_filtered_out():
    accs = [{"id": 1, "geo_country": "RU"}, {"id": 2, "geo_country": "US"}]
    awake = [a["id"] for a in accounts_awake(accs, _NIGHT_MSK)]
    assert 1 not in awake, "у российского аккаунта 04:00 — писать нельзя"
    assert 2 in awake, "у американского в это время день"


def test_all_awake_during_day():
    accs = [{"id": 1, "geo_country": "RU"}, {"id": 2, "geo_country": "US"}]
    assert len(accounts_awake(accs, _MIDDAY_BOTH)) == 2


def test_unknown_geo_is_treated_as_awake():
    """Молча вставать из-за незаполненной страны прокси нельзя."""
    accs = [{"id": 1, "geo_country": None}, {"id": 2}]
    assert len(accounts_awake(accs, _NIGHT_MSK)) == 2


def test_garbage_geo_does_not_crash():
    assert len(accounts_awake([{"id": 1, "geo_country": 12345}], _NIGHT_MSK)) == 1
    assert len(accounts_awake([{"id": 1, "geo_country": "не-страна"}], _NIGHT_MSK)) == 1


def test_empty_list():
    assert accounts_awake([], _NIGHT_MSK) == []
    assert accounts_awake(None, _NIGHT_MSK) == []


def test_does_not_mutate_input():
    accs = [{"id": 1, "geo_country": "RU"}, {"id": 2, "geo_country": "US"}]
    accounts_awake(accs, _NIGHT_MSK)
    assert len(accs) == 2


# ── Подключение к циклу отправки ──────────────────────────────────────────────

def _run_campaign_src() -> str:
    src = (_ROOT / "services" / "dm_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_campaign":
            seg = ast.get_source_segment(src, node)
            assert seg is not None
            return seg
    raise AssertionError("run_campaign не найдена")


def test_send_loop_respects_quiet_hours():
    src = _run_campaign_src()
    assert "accounts_awake(" in src and "_quiet_hours" in src


def test_quiet_hours_on_by_default():
    """Безопасное поведение должно быть умолчанием, а не опцией «для знающих»."""
    src = _run_campaign_src()
    assert '.get("quiet_hours", True) is not False' in src


def test_night_wait_stays_cancellable():
    """Ожидание утра не должно превращаться в неубиваемую операцию: пауза и
    отмена обязаны срабатывать и ночью."""
    src = _run_campaign_src()
    night = src[src.index("accounts_awake(acc_cycle)"):]
    night = night[:2500]
    assert "asyncio.sleep(60)" in night, "ожидание обязано дробиться, а не спать до утра одним куском"
    assert "paused" in night and "cancelled" in night


def test_night_wait_has_safety_cap():
    """Кривое гео у всех аккаунтов не должно останавливать кампанию навсегда."""
    src = _run_campaign_src()
    assert "_QUIET_MAX_WAIT_S" in src
    assert 3600 <= _QUIET_MAX_WAIT_S <= 24 * 3600


def test_ui_exposes_the_setting():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="cmpQuiet"' in ui, "скрытая настройка — не настройка"
    assert "quiet_hours" in ui, "переключатель обязан доезжать до бэкенда"


def test_api_only_persists_explicit_opt_out():
    """Умолчание живёт в движке — API не должен его дублировать."""
    api = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert 'body.get("quiet_hours") is False' in api
