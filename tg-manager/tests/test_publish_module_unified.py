"""Объединение публикации/рассылки в один модуль (без дублей входов).

Владелец: «Быстрый пост», «Массовая публикация» и «Рассылки» — один функционал,
разбитый на три модуля. Объединено в один экран «Публикация / Рассылка»:
  • выбор получателя вверху (подписчики бота / ЛС ведут в свои потоки — разный
    транспорт доставки);
  • публикация в каналы с режимом «Все / Выбрать вручную» — режим «Выбрать»
    поглощает бывший «Быстрый пост» (тот же backend mass_publish);
  • отдельные входы «Быстрый пост» убраны из плиток/дашборда/лаунчера.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_single_screen_has_recipient_selector():
    # Экран публикации ведёт в оба смежных потока рассылки (единый вход).
    i = HTML.index('id="s-masspub"')
    j = HTML.index('</div>\n\n<!--', i) if '</div>\n\n<!--' in HTML[i:] else i + 4000
    seg = HTML[i:i + 4000]
    assert 'openBcastPick()' in seg, "единый экран ведёт к рассылке подписчикам бота"
    assert 'openDmCampaigns()' in seg, "единый экран ведёт к рассылке в ЛС"
    assert 'id="mpScope"' in seg and 'mpScopeToggle()' in seg, "есть выбор Все/Выбрать каналы"
    assert 'id="mpChannels"' in seg, "есть список каналов для ручного выбора"


def test_manual_pick_absorbs_quick_post():
    # sendMassPublish умеет режим ручного выбора (channel_ids из отмеченных).
    i = HTML.index("async function sendMassPublish()")
    j = HTML.index("\nasync function ", i + 1)
    body = HTML[i:j]
    assert "scope === 'pick'" in body
    assert "pickedIds = [...MP_PICKED]" in body
    assert "channel_ids = pickedIds" in body


def test_quick_post_entry_points_removed():
    # Ни одной кнопки/плитки/пункта, ведущих на openQuickPost, не осталось —
    # быстрый пост поглощён единым экраном.
    assert 'onclick="openQuickPost()"' not in HTML
    assert "fn:'openQuickPost'" not in HTML
    # плитки-дубли «Быстрый пост» тоже убраны
    assert HTML.count(">Быстрый пост<") == 0


def test_single_publish_tile_label():
    # Осталась одна метка входа — «Публикация / рассылка», без раздельных
    # «Массовая публикация» + «Быстрый пост» плиток.
    assert "Публикация / рассылка" in HTML
