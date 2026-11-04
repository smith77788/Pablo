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
