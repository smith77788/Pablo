"""Рассылки: у каждой кнопки своё место, и ботов для рассылки можно выбрать.

Жалоба владельца (05.11.2026, экран «📨 Рассылки»):
  * «💬 В чаты» открывала тот же экран «🚀 Всё сразу», что и соседняя кнопка,
    с тем же заголовком и всеми четырьмя ресурсами — две кнопки вели в одно
    место, и было непонятно, куда попал;
  * «🕸 Всем ботам» умела только рассылку по ВСЕМ ботам сразу, хотя сервер
    (`network_broadcast`, ключ `bot_ids`) выбор ботов умел давно: на экране не
    было списка. В кампании «Всё сразу» выбора ботов тоже не было, хотя
    `campaign_hub` принимает сегмент `selected_bots`.
"""
from __future__ import annotations

import re

from tests.miniapp_source import js_function, miniapp_html


def test_chats_button_opens_its_own_mode_not_the_all_at_once_screen():
    html = miniapp_html()
    assert "onclick=\"openUniCampaign('chats')\"" in html
    body = js_function("openUniCampaign")
    # Свой заголовок у одиночного режима…
    assert "ucTitle" in body and "UC_KIND_TITLE" in body
    assert re.search(r"chats\s*:\s*'💬 В чаты'", html)
    # …и только свой ресурс: строки остальных прячутся.
    assert "uc-kind-row" in body and "display" in body
    for kid in ("ucKChannels", "ucKChats", "ucKBots", "ucKDm"):
        assert f'data-uc-kind="{kid}"' in html


def test_network_broadcast_modal_lets_pick_bots():
    html = miniapp_html()
    assert 'id="netbcBots"' in html
    assert "_netbcLoadBots()" in js_function("openNetBcModal")
    send = js_function("sendNetBc")
    # Не все отмечены — уходит список; ни одного — отказ до сервера.
    assert "bot_ids: picked" in send
    assert "Отметьте хотя бы одного бота" in send
    assert "Всем ботам</button>" not in html


def test_campaign_bots_can_be_picked_by_hand():
    html = miniapp_html()
    assert '<option value="selected_bots">' in html
    assert 'id="ucBots"' in html
    spec = js_function("_ucSpec")
    assert "selected_bot_ids" in spec
    assert "selected_bot_ids" in js_function("launchUniCampaign")


def test_campaign_hub_accepts_the_segment_the_screen_sends():
    from services import campaign_hub
    assert "selected_bots" in campaign_hub.ALLOWED_BOT_SEGMENTS
