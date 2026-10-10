"""Фабрики каналов и групп ведут в конструктор гео-сети.

Жалоба владельца: «Фабрики каналов/чатов не умеют создавать гео-сети». Движок
гео-сети (global_presence) существует и умеет создавать каналы/группы по
городам, но из фабрик к нему не было ни одной кнопки — владелец не находил, как
собрать сеть по регионам. Дублировать движок в фабрике нельзя (анти-дубль),
поэтому обе фабрики ведут в тот же конструктор через GeoPresenceCb(action=menu).

Тест держит переход на месте: без него кнопка легко теряется при следующей
перетряске меню.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_channel_factory_menu_links_to_geo_network():
    src = _src("bot/handlers/channel_factory.py")
    assert "GeoPresenceCb" in src, "фабрика каналов не импортирует переход в гео-сеть"
    assert re.search(r"Гео-сеть[^\"]*\",\s*callback_data=GeoPresenceCb\(action=\"menu\"\)",
                     src), "в меню фабрики каналов нет кнопки гео-сети"


def test_group_factory_menu_links_to_geo_network():
    src = _src("bot/handlers/group_factory.py")
    assert "GeoPresenceCb" in src, "фабрика групп не импортирует переход в гео-сеть"
    assert re.search(r"Гео-сеть[^\"]*\",\s*callback_data=GeoPresenceCb\(action=\"menu\"\)",
                     src), "в меню фабрики групп нет кнопки гео-сети"


def test_geo_network_menu_handler_exists():
    """Кнопка ведёт в реальный обработчик, а не в пустоту."""
    gp = _src("bot/handlers/global_presence.py")
    assert 'GeoPresenceCb.filter(F.action == "menu")' in gp, \
        "обработчик меню гео-сети отсутствует — кнопки фабрик вели бы в никуда"
