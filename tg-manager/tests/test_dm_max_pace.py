"""Режим «максимально быстро» (pace=max) у DM-рассылок.

Было: slow/normal/fast/auto — самого быстрого режима не хватало. Добавлен
`max`: минимум пауз (×0.03 от человеческих задержек), но жёсткие
предохранители (FloodWait/карантин/губернатор/дневной лимит) НЕ отключаются.

Тест сторожит сквозную цепочку: множитель в движке < fast, оценка срока
короче fast, оба роута (создание и правка) принимают max, и опция есть в UI.
"""
from __future__ import annotations

import re

from services.dm_engine import _PACE_MULTIPLIERS, estimate_duration_seconds

from tests.miniapp_source import api_function, miniapp_html

# Источник API-слоя — через api_function (тело ручки по точным границам в любом
# из mini_app_*.py), а не чтением mini_app_api.py по пути: иначе проверка
# краснеет при первом же выносе ручки из монолита, а не когда она ломается
# (tests/test_tests_do_not_pin_the_monolith.py). Разметку берём из miniapp_html.
HTML = miniapp_html()


def test_max_is_fastest_multiplier():
    assert "max" in _PACE_MULTIPLIERS, "нет режима max в _PACE_MULTIPLIERS"
    # max строго быстрее fast, а fast быстрее normal — порядок темпов сохранён.
    assert _PACE_MULTIPLIERS["max"] < _PACE_MULTIPLIERS["fast"] < _PACE_MULTIPLIERS["normal"]
    # Но не ноль: совсем без паузы — это мгновенные баны, а не «быстро».
    assert _PACE_MULTIPLIERS["max"] > 0


def test_max_estimate_shorter_than_fast_but_positive():
    n = 1000
    eta_max = estimate_duration_seconds("contacts", "max", n)
    eta_fast = estimate_duration_seconds("contacts", "fast", n)
    assert 0 < eta_max < eta_fast


def test_both_dm_routes_accept_max():
    # Создание рассылки (dm_campaign_create) и правка кампании
    # (dm_campaign_update) — оба whitelist'а должны пропускать max, иначе он
    # молча падает в normal / 400. Берём тело каждой ручки отдельно: проверка
    # «оба» обязана видеть обе, а не одну на весь слой.
    whitelist = '("slow", "normal", "fast", "max", "auto")'
    assert whitelist in api_function("dm_campaign_create")
    assert whitelist in api_function("dm_campaign_update")


def test_ui_has_max_option():
    assert 'value="max"' in HTML and "Максимально быстро" in HTML


def test_adhoc_delay_floor_allows_max_speed():
    """Разовая рассылка — часть того же модуля: её минимальная задержка опущена
    до 1с (было 5), чтобы «максимально быстро» достигалось и здесь. 1, а не 0:
    губернатор стретчит базу МУЛЬТИПЛИКАТИВНО, при нуле он бы не тормозил."""
    assert "min_val=1, max_val=600" in api_function("dm_adhoc_send"), \
        "порог задержки разовой рассылки должен быть 1с"
    # UI-поле тоже должно позволять 1 (иначе ввод min=5 ограничит сверху вниз)
    assert 'id="daDelay"' in HTML
    m = re.search(r'id="daDelay"[^>]*', HTML)
    assert m and 'min="1"' in m.group(0), "поле daDelay должно допускать min=1"
