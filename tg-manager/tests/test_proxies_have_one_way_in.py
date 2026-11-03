"""У прокси один вход: плитка «Прокси», а срезы парка — тапом по счётчику.

В каталоге стояли две плитки на одни и те же данные (`/api/miniapp/proxies`):
«Прокси» (добавить, импорт, проверка, изоляция, ротация, failover) и «Пул
прокси» (счётчики, срезы живые/мёртвые/резерв, поиск, задержки). Владелец не
знал, куда идти, и половину умений одного экрана не находил на другом.

Теперь плитка одна. Над списком «Прокси» — серверные счётчики парка, и каждый
открывает поиск с этим срезом.
"""
from __future__ import annotations

import re

from tests.miniapp_source import js_function, miniapp_html


def test_one_tile_for_proxies():
    html = miniapp_html()
    tiles = re.findall(r'class="mgmt-tile"[^>]*onclick="(openProxies|openProxyPool)\(', html)
    assert tiles == ["openProxies"], f"плиток прокси: {tiles}"


def test_counters_open_the_pool_with_their_slice():
    kpi = js_function("_renderProxyKpi")
    for f in ("all", "alive", "dead"):
        assert f"'{f}'" in kpi, f"счётчик среза {f} не открывается"
    assert "openProxyPool(" in kpi
    assert "d.counts" in kpi, "счётчики должны быть серверными, а не по странице"
    assert "_renderProxyKpi(" in js_function("loadProxies")


def test_the_pool_honours_the_requested_slice():
    body = js_function("openProxyPool")
    assert "PP_FILTER = want" in body, "срез из счётчика не применяется"
    assert "['all','alive','dead','backup'].includes(filter)" in body, (
        "незнакомый срез должен откатываться к «Все», а не пустому списку")
