"""Выбор аккаунтов показывает, кто это, в каком он состоянии и сколько у него ботов.

Живой обход мини-аппа (03.11.2026): в фабрике ботов аккаунты для @BotFather
выбирались по одному номеру телефона — без имени, без статуса (заморожен,
спам-блок) и без числа уже созданных ботов, хотя у @BotFather потолок 20.
Выбор такого аккаунта давал гарантированный провал цели. Выбор общий (им же
пользуется «Написать контакту»), поэтому статус теперь виден везде, а лимит
ботов — в фабрике. «Выбрать все» неактивные аккаунты не берёт.
"""
from __future__ import annotations

from tests.miniapp_source import api_function, js_function, miniapp_source


def test_accounts_list_returns_bots_owned():
    assert "AS bots_owned" in api_function("accounts")


def test_picker_explains_and_disables_useless_accounts():
    body = js_function("_renderContactAccPicker")
    assert "accRu(st)" in body, "статус аккаунта не показан"
    assert "BOTFATHER_BOT_CAP" in body and "bots_owned" in body
    assert "disabled" in body


def test_bot_factory_asks_for_the_limit():
    src = miniapp_source()
    assert "_renderContactAccPicker('bfAccts', [], {botLimit: true})" in src


def test_select_all_skips_disabled_accounts():
    for fn in ("bfAccAll",):
        assert "!cb.disabled" in js_function(fn)
    assert "#cfAccts .ca-acc-cb').forEach(cb => { cb.checked = !!on && !cb.disabled; })" in miniapp_source()
