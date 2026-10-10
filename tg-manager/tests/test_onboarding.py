"""Онбординг-чеклист: чистая логика активации + подключение эндпоинта/фронта."""
from __future__ import annotations

import os

from services import onboarding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_fresh_user_all_steps_open_not_activated():
    r = onboarding.build_checklist(accounts=0, accounts_with_proxy=0, proxies=0, ops_total=0)
    assert r["activated"] is False
    assert r["done"] == 0 and r["total"] == 3
    assert r["next_step"] == "accounts"          # фокус на первом обязательном
    ids = [s["id"] for s in r["steps"]]
    assert ids == ["accounts", "proxy", "first_op"]


def test_proxy_is_optional_and_not_blocking_activation():
    # аккаунты есть + первое действие есть, прокси НЕТ → всё равно activated
    r = onboarding.build_checklist(accounts=3, accounts_with_proxy=0, proxies=0, ops_total=1)
    assert r["activated"] is True
    proxy = next(s for s in r["steps"] if s["id"] == "proxy")
    assert proxy["optional"] is True and proxy["done"] is False


def test_proxy_done_via_account_binding_or_pool():
    r1 = onboarding.build_checklist(accounts=1, accounts_with_proxy=1, proxies=0, ops_total=0)
    r2 = onboarding.build_checklist(accounts=1, accounts_with_proxy=0, proxies=2, ops_total=0)
    for r in (r1, r2):
        assert next(s for s in r["steps"] if s["id"] == "proxy")["done"] is True


def test_next_step_advances_as_steps_complete():
    r = onboarding.build_checklist(accounts=2, accounts_with_proxy=0, proxies=0, ops_total=0)
    assert r["activated"] is False
    assert r["next_step"] == "first_op"          # аккаунты закрыты → следующий обязательный
    assert next(s for s in r["steps"] if s["id"] == "accounts")["done"] is True


def test_activated_user_no_open_core_steps():
    r = onboarding.build_checklist(accounts=5, accounts_with_proxy=5, proxies=3, ops_total=10)
    assert r["activated"] is True and r["done"] == 3 and r["next_step"] is None


def test_negative_or_none_counts_are_safe():
    r = onboarding.build_checklist(accounts=None, accounts_with_proxy=-1, proxies=None, ops_total=None)
    assert r["activated"] is False and r["done"] == 0


def test_endpoint_and_route_wired():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def onboarding_status" in src
    assert '"/api/miniapp/onboarding"' in src
    assert "onboarding.build_checklist" in src


def test_frontend_card_wired():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="onboardCard"' in html
    assert "function loadOnboarding" in html
    assert "/api/miniapp/onboarding" in html
    # действия шагов ведут в существующие потоки
    assert "openAccImportModal" in html and "openProxies" in html and "openMassInvite" in html


def test_every_step_kind_is_handled_in_frontend():
    """Гейт: каждый шаг чеклиста несёт action.kind, который фронт разворачивает
    через _ONBOARD_ACT. Легко добавить шаг и забыть ветку → кнопка шага мёртвая."""
    import re

    checklist = onboarding.build_checklist(
        accounts=0, accounts_with_proxy=0, proxies=0, ops_total=0
    )
    kinds = {s["action"]["kind"] for s in checklist["steps"] if s.get("action")}
    assert kinds, "у шагов нет action.kind — сломался контракт"

    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    i = html.find("const _ONBOARD_ACT")
    assert i != -1, "_ONBOARD_ACT не найден во фронте"
    body = html[i:i + 600]
    for k in sorted(kinds):
        assert re.search(rf"\b{k}\s*:", body), (
            f"шаг '{k}' не обработан в _ONBOARD_ACT — тап по кнопке шага мёртвый"
        )
