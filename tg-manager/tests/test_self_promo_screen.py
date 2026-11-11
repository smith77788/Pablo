"""Экран «📣 Самопиар»: свои каналы и боты, а не только реклама платформы.

Владелец назвал модуль бесполезным (09.11.2026): экран умел одно — разослать
подписчикам рекламу самой платформы. Что держит файл:

* у экрана есть кампании по своим ботам и каналам, идеи и редактор;
* число получателей спрашивается у сервера ДО запуска, и подтверждение его
  называет — вместе с тем, что сообщения в личку не отозвать;
* запуск без подтверждения невозможен ни из списка, ни из редактора;
* есть пробная отправка себе;
* прежний блок рекламы платформы остался доступен (свёрнут);
* автопоказ новым подписчикам запускается фоновым циклом в main.py.
"""
from __future__ import annotations

import pathlib
import re

from tests.miniapp_source import js_function, miniapp_html

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_screen_has_campaigns_ideas_and_an_editor():
    html = miniapp_html()
    for el in ('id="spcList"', 'id="spcIdeas"', 'id="spcKpis"', 'id="s-spc-edit"',
               'id="spcPreview"', 'id="spcAuto"', 'id="spcCooldown"', 'id="spcSkip"'):
        assert el in html, el
    assert "spcLoad()" in js_function("openSelfPromo")
    assert 'onclick="spcOpenEditor()"' in html
    assert "/api/miniapp/selfpromo/overview" in js_function("spcLoad")


def test_a_launch_names_the_exact_audience_before_asking():
    f = js_function("_spcConfirmAndLaunch")
    assert f.index("selfpromo/preview") < f.index("askConfirm(") < f.index("/launch")
    assert "_spcCountsText(counts)" in f and "отозвать нельзя" in f
    # Некому слать — не спрашиваем «запустить?», а объясняем почему.
    assert "showInfo(" in f
    text = js_function("_spcCountsText")
    for reason in ("уже получали эту кампанию", "уже подписаны на продвигаемого бота",
                   "недавно получали другой самопиар"):
        assert reason in text
    # Точные числа, без сокращений вида «1,0К».
    assert "num(" not in text


def test_every_way_to_launch_goes_through_the_confirmation():
    assert "_spcConfirmAndLaunch(" in js_function("spcLaunch")
    assert "_spcConfirmAndLaunch(" in js_function("spcSave")
    src = (ROOT / "mini_app" / "screens" / "selfpromo.js").read_text(encoding="utf-8")
    assert len(re.findall(r"/launch`", src)) == 1, "запуск мимо подтверждения"


def test_there_is_a_test_send_and_text_help():
    assert "/api/miniapp/selfpromo/test" in js_function("spcTest")
    f = js_function("spcPickText")
    assert "/api/miniapp/selfpromo/texts" in f and "askChoice(" in f


def test_the_platform_promo_block_is_still_reachable():
    html = miniapp_html()
    assert 'id="spList"' in html and 'id="spRefLink"' in html
    assert "Реклама Infragram по вашей реферальной ссылке" in html
    assert 'onclick="openSelfPromoCreateModal()"' in html


def test_auto_show_is_started_with_the_process():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '_resilient("self_promo_auto", _self_promo.run, pool)' in main
