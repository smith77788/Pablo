"""Верхняя лента KPI должна вести в ПРАВИЛЬНЫЕ разделы.

Баг: клик по «Каналов» открывал «Аккаунты» — все плитки шли через goTab, а
Каналы/Операции — отдельные экраны (openChannels/openOps), не вкладки.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _act_for(label: str) -> str:
    m = re.search(re.escape(f"lbl:'{label}'") + r",\s*act:\"([^\"]+)\"", HTML)
    assert m, f"плитка {label} не найдена"
    return m.group(1)


def test_channels_chip_opens_channels_not_accounts():
    assert _act_for("📡 Каналов") == "openChannels()"


def test_ops_chips_open_ops_screen():
    for lbl in ("⚙️ В работе", "⏳ В очереди", "❌ Ошибок 24ч"):
        assert _act_for(lbl) == "openOps()"


def test_tab_chips_use_valid_tabs():
    assert _act_for("🤖 Ботов") == "goTab('bots')"
    assert _act_for("📱 Аккаунтов") == "goTab('accounts')"


def test_target_functions_exist():
    assert "function openChannels()" in HTML
    assert "function openOps()" in HTML
