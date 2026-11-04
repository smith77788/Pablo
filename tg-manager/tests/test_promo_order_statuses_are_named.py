"""Каждый статус заказа продвижения, который знает бот, мини-апп называет по-русски.

Заказ получил короткий статус 'launching' (занят на время запроса к
SMM-панели), а словарь ORDER_STATUS мини-аппа его не знал: в строке заказа
появлялось сырое английское слово. Источник списка статусов — словарь бота
`_ORDER_STATUS`; разойтись им теперь не даёт этот тест.
"""
from __future__ import annotations

import re

from bot.handlers.promo_platform import _ORDER_STATUS
from tests.miniapp_source import miniapp_source


def _miniapp_order_statuses() -> set[str]:
    m = re.search(r"const ORDER_STATUS = \{([^}]*)\}", miniapp_source())
    assert m, "словарь ORDER_STATUS не найден в мини-аппе"
    return set(re.findall(r"(\w+)\s*:", m.group(1)))


def test_every_bot_status_has_a_miniapp_label():
    missing = set(_ORDER_STATUS) - _miniapp_order_statuses()
    assert not missing, f"мини-апп покажет сырое слово для статусов: {sorted(missing)}"


def test_launching_is_named():
    assert "launching" in _miniapp_order_statuses()
