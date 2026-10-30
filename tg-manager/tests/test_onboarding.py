"""Онбординг-чеклист: чистая логика «до первого результата» + подключение
эндпоинта/фронта/бота. Поведение на живой базе — test_onboarding_e2e_postgres.py."""
from __future__ import annotations

import os
import re

from services import onboarding
from tests.miniapp_source import api_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _step(r, sid):
    return next(s for s in r["steps"] if s["id"] == sid)


def test_fresh_user_all_steps_open_not_activated():
    r = onboarding.build_checklist(accounts=0, accounts_alive=0, plan_paid=False)
    assert r["activated"] is False
    assert r["done"] == 0 and r["total"] == 5
    assert r["next_step"] == "accounts"
    assert [s["id"] for s in r["steps"]] == [
        "accounts", "proxy", "plan", "first_op", "first_result"]


def test_account_alone_does_not_activate():
    """P0 D8: аккаунт подключён (и платформа сама поставила contacts_push),
    но владелец ничего не запускал — чеклист обязан остаться."""
    r = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True)
    assert r["activated"] is False
    assert _step(r, "accounts")["done"] is True
    assert r["next_step"] == "first_op"


def test_launched_op_is_not_a_result():
    r = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True,
                                   user_ops=1)
    assert r["activated"] is False
    assert _step(r, "first_op")["done"] is True
    assert r["next_step"] == "first_result"


def test_inflight_op_shows_progress_and_opens_it():
    r = onboarding.build_checklist(
        accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
        inflight_op={"id": 42, "op_type": "parse_audience", "label": "Сбор @x",
                     "done_items": 3, "total_items": 10})
    st = _step(r, "first_result")
    assert st["done"] is False
    assert "Сбор @x" in st["hint"] and "3 из 10" in st["hint"]
    assert st["action"] == {"kind": "operation", "param": 42}


def test_failed_op_points_to_recovery_without_secrets():
    r = onboarding.build_checklist(
        accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
        last_failed_op={"id": 7, "op_type": "mass_invite", "label": "",
                        "status": "failed",
                        "reason": "нет прав postgresql://u:secretpw@h/db"})
    st = _step(r, "first_result")
    assert st["done"] is False and r["activated"] is False
    assert st["action"] == {"kind": "operation", "param": 7}
    assert "secretpw" not in st["hint"]
    assert "Массовый инвайт" in st["hint"]  # описание из OP_REGISTRY, когда нет label


def test_productive_op_activates():
    r = onboarding.build_checklist(
        accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
        first_result={"id": 9, "op_type": "parse_audience", "label": "Сбор", "ok": 120})
    assert r["activated"] is True and r["next_step"] is None
    assert r["first_result"] == {"op_id": 9, "title": "Сбор", "ok": 120}


def test_warmup_day_is_a_result():
    r = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True,
                                   warmup_days=1)
    assert r["activated"] is True
    assert r["first_result"]["title"] == "Пройден день прогрева"


def test_dead_accounts_do_not_close_the_step():
    r = onboarding.build_checklist(accounts=3, accounts_alive=0, plan_paid=True)
    st = _step(r, "accounts")
    assert st["done"] is False
    assert "ни один не рабочий" in st["hint"]


def test_proxy_is_optional_and_not_blocking():
    r = onboarding.build_checklist(
        accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
        first_result={"id": 1, "op_type": "x", "label": "x", "ok": 1})
    assert r["activated"] is True
    proxy = _step(r, "proxy")
    assert proxy["optional"] is True and proxy["done"] is False


def test_proxy_done_via_account_binding_or_pool():
    for kw in ({"accounts_with_proxy": 1}, {"proxies": 2}):
        r = onboarding.build_checklist(accounts=1, **kw)
        assert _step(r, "proxy")["done"] is True


def test_plan_step_free_user_and_trial():
    free = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=False)
    assert _step(free, "plan")["done"] is False
    assert _step(free, "plan")["action"] == {"kind": "billing"}
    assert free["next_step"] == "plan"

    avail = onboarding.build_checklist(accounts=0, plan_paid=False, trial_available=True)
    assert "Пробный период" in _step(avail, "plan")["title"]
    assert avail["next_step"] == "accounts"

    on = onboarding.build_checklist(accounts=1, accounts_alive=1, plan_paid=True,
                                    trial={"days_left": 2, "expires_at": None})
    assert _step(on, "plan")["done"] is True
    assert "осталось 2 дн." in _step(on, "plan")["title"]
    assert on["trial"]["days_left"] == 2


def test_negative_or_none_counts_are_safe():
    r = onboarding.build_checklist(accounts=None, accounts_alive=-1,
                                   accounts_with_proxy=-1, proxies=None,
                                   plan_paid=False, user_ops=None)
    assert r["activated"] is False and r["done"] == 0


def test_system_and_service_ops_are_not_results():
    assert "contacts_push" in onboarding.NOT_A_RESULT
    assert "contacts_sync" in onboarding.NOT_A_RESULT
    assert "check_accounts_health" in onboarding.NOT_A_RESULT
    for real in ("mass_invite", "parse_audience", "account_warmup", "bulk_dm_adhoc"):
        assert real not in onboarding.NOT_A_RESULT


def test_endpoint_and_route_wired():
    src = api_source()
    assert "async def onboarding_status" in src
    assert '"/api/miniapp/onboarding"' in src
    assert "onboarding.load_checklist" in src
    # P0 D8: эндпоинт больше не считает «активацией» любую строку очереди.
    i = src.index("async def onboarding_status")
    body = src[i:src.index("\n    async def ", i + 10)]
    assert "operation_queue" not in body


def _html():
    return open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def test_frontend_card_wired():
    html = _html()
    assert 'id="onboardCard"' in html
    assert "function loadOnboarding" in html
    assert "/api/miniapp/onboarding" in html
    for fn in ("openAccImportModal", "openProxies", "openBilling", "openMassOps",
               "function onboardFirstAction", "openWarmup", "openParser"):
        assert fn in html


def test_every_step_kind_is_handled_in_frontend():
    """Гейт: каждый action.kind чеклиста разворачивается через _ONBOARD_ACT.
    Легко добавить шаг и забыть ветку → кнопка шага мёртвая."""
    states = [
        dict(accounts=0, plan_paid=False),
        dict(accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
             inflight_op={"id": 1, "label": "x"}),
        dict(accounts=1, accounts_alive=1, plan_paid=True, user_ops=1,
             last_failed_op={"id": 1, "label": "x"}),
        dict(accounts=1, accounts_alive=1, plan_paid=True, user_ops=1),
    ]
    kinds = set()
    for st in states:
        for s in onboarding.build_checklist(**st)["steps"]:
            if s.get("action"):
                kinds.add(s["action"]["kind"])
    assert {"import_accounts", "proxy", "billing", "first_action", "operation"} <= kinds

    html = _html()
    i = html.find("const _ONBOARD_ACT")
    assert i != -1, "_ONBOARD_ACT не найден во фронте"
    body = html[i:i + 600]
    for k in sorted(kinds):
        assert re.search(rf"\b{k}\s*:", body), f"шаг '{k}' не обработан в _ONBOARD_ACT"


def test_bot_deeplinks_are_handled_in_frontend():
    html = _html()
    assert "k==='onboarding'" in html and "k==='accounts_add'" in html
    src = open(os.path.join(ROOT, "bot", "handlers", "start.py"), encoding="utf-8").read()
    assert "#onboarding" in src
    assert "_onboarding_message" in src
