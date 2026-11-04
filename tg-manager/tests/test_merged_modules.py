"""Дубли модулей не возвращаются.

В каталоге «Разделы» накопились пары и тройки плиток, которые открывали одно
и то же или половину одного и того же. Их слили; здесь — по проверке на
каждое слияние, чтобы дубль не вернулся «на всякий случай». Модуль «Поиск
Telegram» — отдельный файл `test_search_module_is_one.py`.
"""
from __future__ import annotations

import re

from services.task_sections import section_of_tile
from tests.miniapp_source import miniapp_html, miniapp_source

HTML = miniapp_html()
SRC = miniapp_source()


def _catalog_labels() -> list[str]:
    s = HTML.index('<div class="screen" id="s-more">')
    cat = HTML[s:HTML.index("<!-- Админ-панель (только для админов) -->", s)]
    return re.findall(r'mgmt-tile-lbl">([^<]*)<', cat)


def _fn_body(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\([^)]*\)\s*\{", SRC)
    assert m, f"функции {name} нет"
    depth, i = 0, m.end() - 1
    while True:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
        if depth == 0:
            return SRC[m.start():i]


def test_autoreg_is_one_screen():
    """«Авторег» и «Авторег Pro» ходили в одну ручку /api/miniapp/autoreg."""
    labels = _catalog_labels()
    assert labels.count("Авторег") == 1
    assert "Авторег Pro" not in labels and section_of_tile("Авторег Pro") is None
    assert 'id="s-autoreg-pro"' not in HTML
    assert not re.search(r"function\s+(openAutoregPro|submitAutoreg)\b", SRC)
    assert "push('s-autoreg')" in _fn_body("openAutoreg")


def test_autoreg_form_promises_only_what_the_server_does():
    """Сервер берёт из запроса только count (до 50) и country. Выпадашки
    «SMS-сервис» и «Прокси» и предел «до 100» ничего не меняли."""
    body = _fn_body("submitAutoregPro")
    assert "JSON.stringify({count, country})" in body
    assert "count>50" in body
    assert "Списываются средства" in body, "подтверждение молчит о деньгах за номера"
    assert 'id="arp-service"' not in HTML and 'id="arp-proxy"' not in HTML


def test_factory_is_one_module():
    """Три фабрики — одна плитка «Фабрика» с вкладками; каналы и группы —
    один экран с галочкой, и вкладка «Группы» не стирает набранное."""
    labels = _catalog_labels()
    assert labels.count("Фабрика") == 1
    for old in ("Фабрика ботов", "Фабрика каналов", "Фабрика групп"):
        assert old not in labels and section_of_tile(old) is None, old
    reg = re.search(r"const SCREEN_MODULES = \{(.*?)\n\};", SRC, re.S).group(1)
    for key, sid in (("bots", "s-botfactory"), ("channels", "s-chanfactory"),
                     ("groups", "s-chanfactory")):
        assert re.search(r"key:'" + key + r"'[^\n]*sid:'" + sid + "'", reg), key
    for sid, key in (("s-botfactory", "bots"), ("s-chanfactory", "channels")):
        head = HTML[HTML.index(f'<div class="screen" id="{sid}">'):][:900]
        assert '<div class="hdr-title">🏭 Фабрика</div>' in head, sid
        assert f'data-module="factory" data-tab="{key}"' in head, sid
    cf = _fn_body("_cfTab")
    assert "STACK[STACK.length - 1] !== 's-chanfactory'" in cf and "openChannelFactory(asGroup)" in cf
    assert "_markModTab('factory'" in _fn_body("openChannelFactory"), (
        "старый вход «создать группы» откроет экран с подсвеченной вкладкой «Каналы»")


def _assert_module(mod: str, title: str, tile: str, tabs: dict[str, tuple[str, str]],
                   gone: tuple[str, ...]) -> None:
    """Одна плитка модуля, старые плитки сняты, каждая вкладка — живой экран
    со своей open-функцией, общей шапкой и полоской вкладок."""
    labels = _catalog_labels()
    assert labels.count(tile) == 1, tile
    for old in gone:
        assert old not in labels and section_of_tile(old) is None, f"«{old}» вернулась"
    reg = re.search(r"const SCREEN_MODULES = \{(.*?)\n\};", SRC, re.S).group(1)
    block = reg[reg.index(f"  {mod}: {{"):]
    block = block[:block.index("\n  },")]
    assert f"title: '{title}'" in block
    for key, (sid, fn) in tabs.items():
        assert re.search(r"key:'" + key + r"'[^\n]*sid:'" + sid + r"'[^\n]*" + fn + r"\(", block), key
        assert f"push('{sid}')" in _fn_body(fn), f"{fn}() больше не открывает {sid}"
        if f'<div class="screen" id="{sid}">' in HTML:
            head = HTML[HTML.index(f'<div class="screen" id="{sid}">'):][:1200]
            assert f'<div class="hdr-title">{title}</div>' in head, sid
            assert f'data-module="{mod}" data-tab="{key}"' in head, sid
        else:  # экран, который собирает JS (s-uni-dash)
            assert f'data-module="{mod}" data-tab="{key}"' in SRC, sid


def test_dashboard_and_summary_are_one_module():
    _assert_module("overview", "📊 Дашборд", "Дашборд",
                   {"now": ("s-uni-dash", "openUnifiedDashboard"),
                    "days": ("s-dashboard", "openDashboard")},
                   gone=("Сводка",))
    # Экран дашборда рождается после загрузки: без повторного рендера у него
    # была бы пустая полоска вкладок.
    assert "_renderModuleTabs()" in _fn_body("_udEnsureScreen")


def test_health_is_one_module():
    _assert_module("health", "❤️ Здоровье", "Здоровье",
                   {"accounts": ("s-health", "openHealth"),
                    "infra": ("s-infrahealth", "openInfraHealth"),
                    "diag": ("s-diag", "openDiag")},
                   gone=("Дэшборд здоровья", "Здоровье инфры", "Диагностика"))
