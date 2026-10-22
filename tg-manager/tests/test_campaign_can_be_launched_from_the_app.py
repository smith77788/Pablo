"""Единую кампанию можно запустить из мини-аппа, и выбор в экране движок поймёт.

НАХОДКА (22.10.2026). `services/campaign_hub.py` — движок единой кампании:
один спек раскладывается на дочерние операции по четырём видам ресурсов
(каналы, чаты аккаунтов, подписчики ботов, ЛС), ставится под общим
campaign_id, отказ отдельной ветки не валит остальные. Он написан, покрыт
тестами (test_campaign_hub_plan, test_campaign_hub_launch_postgres) — и
запустить его было НЕЛЬЗЯ: ручку /api/miniapp/campaign/launch не звал ни один
экран. Планировщик кампании считал план и отправлял владельца заполнять
массовый инвайт руками, теряя и веер по ресурсам, и запись кампании.

Это ровно то, о чём `test_routes_the_owner_can_reach`: бэкенд написан в
пустоту. Здесь — сторож на обратное: дверь есть, и то, что экран предлагает
выбрать, движок принимает.

ПОЧЕМУ СПИСКИ СВЕРЯЮТСЯ С СЕРВЕРОМ, А НЕ ЗАХАРДКОЖЕНЫ. Сегменты рассылки по
ботам и типы аудитории ЛС заданы множествами в campaign_hub (ALLOWED_*), и
движок бросает CampaignSpecError на незнакомом значении. Опечатка в <option>
даёт экран, который выглядит рабочим и падает при запуске — у владельца это
«кампания не стартует», а причина в одной букве.
"""
from __future__ import annotations

import re

from services import campaign_hub as ch
from tests.miniapp_source import js_function, miniapp_html


def _select_values(select_id: str) -> list[str]:
    """Значения <option> конкретного <select> разметки."""
    html = miniapp_html()
    m = re.search(r'<select id="' + re.escape(select_id) + r'"[^>]*>(.*?)</select>',
                  html, re.S)
    assert m, f"в разметке нет <select id=\"{select_id}\">"
    values = re.findall(r'<option value="([^"]*)"', m.group(1))
    assert values, f"у select {select_id} нет вариантов"
    return values


def test_the_engine_has_a_door_at_all():
    """Сам смысл находки: ручку запуска кто-то зовёт."""
    assert "/api/miniapp/campaign/launch" in miniapp_html(), (
        "запустить единую кампанию снова нельзя: ручку не зовёт ни один экран, "
        "хотя движок campaign_hub на месте и покрыт тестами")
    body = js_function("launchUniCampaign")
    assert "/api/miniapp/campaign/launch" in body, "кнопка запуска не зовёт движок"


def test_the_screen_builds_the_spec_the_engine_validates():
    spec = js_function("_ucSpec")
    ret = re.search(r"return \{(.*?)\};", spec, re.S)
    assert ret, "_ucSpec ничего не возвращает"
    for key in ("content", "settings", "targets"):
        assert key in ret.group(1), f"спек кампании уходит без {key}"
    assert "enabled: true" in spec, "ресурсы уходят без признака enabled"
    # Пустой channel_ids движок читает как «во все мои каналы» — значит при
    # выборе «все» список не должен попадать в спек вовсе.
    assert "if (document.getElementById('ucChScope').value === 'pick')" in spec, (
        "выбор «во все каналы» и «выбрать вручную» не различается")
    # plan_campaign отбивает спек без текста и без ресурсов — экран обязан
    # сказать это сам, а не ловить 400 с сервера.
    guard = js_function("launchUniCampaign")
    assert "Введите текст сообщения" in guard, "экран не проверяет текст до запуска"
    assert "хотя бы один ресурс" in guard, "экран не проверяет выбор ресурсов"


def test_every_bot_segment_offered_is_one_the_engine_accepts():
    offered = _select_values("ucBotSeg")
    unknown = sorted(set(offered) - ch.ALLOWED_BOT_SEGMENTS)
    assert not unknown, (
        f"экран предлагает сегменты ботов {unknown}, которых движок не знает: "
        f"campaign_hub бросит CampaignSpecError уже при запуске")


def test_every_dm_audience_offered_is_one_the_engine_accepts():
    offered = _select_values("ucDmTarget")
    unknown = sorted(set(offered) - ch.ALLOWED_DM_TARGETS)
    assert not unknown, (
        f"экран предлагает аудитории ЛС {unknown}, которых движок не знает")
    # Аудитории, которым нужен ещё и выбор бота, когорты или своего списка,
    # экран намеренно НЕ предлагает: для них есть экран «ЛС», где эти поля
    # есть. Запуск с ними отсюда ушёл бы без target_id и упал.
    needs_more = {"bot_users", "cohort", "segment", "parsed_audience", "import_list"}
    assert not (set(offered) & needs_more), (
        f"эти аудитории требуют дополнительного выбора, которого на экране нет: "
        f"{sorted(set(offered) & needs_more)}")


def test_the_launch_asks_before_it_fans_out():
    """Кампания создаёт операции по каждому ресурсу — отменить одной кнопкой нельзя."""
    body = js_function("launchUniCampaign")
    assert "askConfirm(" in body, "кампания уходит без подтверждения"
    assert "_ucSummary(" in body, (
        "подтверждение не называет адресатов: экран массовой публикации уже "
        "учил, что подтверждение без числа — это подпись не глядя")
    summary = js_function("_ucSummary")
    for word in ("каналы", "чаты", "ботов", "ЛС"):
        assert word in summary, f"в сводке подтверждения нет про {word}"


def test_partial_failure_is_not_swallowed():
    """Отказ ветки движок кладёт в errors — промолчать значит соврать, что ушло всё."""
    body = js_function("launchUniCampaign")
    assert "d.errors" in body, "экран не читает errors: часть кампании могла не уйти"
    assert "Не ушло" in body, "частичный отказ не показывается владельцу"
