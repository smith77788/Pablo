"""Экран операций называется так же, как кнопка, которая его открывает.

Плитка на главной и раздел каталога — «Операции», а экран открывался с
заголовком «Диспетчер задач»: человек нажимал одно и попадал, судя по
шапке, в другое. Обход всех плиток 04.11.2026.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html


def test_header_matches_the_tile():
    html = miniapp_html()
    tile = re.search(r'onclick="openOps\(\)"><div class="qa-tile-ico">[^<]*</div>'
                     r'<div class="qa-tile-lbl">([^<]+)</div>', html)
    assert tile, "плитка операций на главной пропала"
    i = html.index('id="s-ops"')
    # hdr-title может нести атрибуты (id="opsTitle" для динамической смены
    # заголовка), поэтому допускаем их между классом и «>».
    head = re.search(r'class="hdr-title"[^>]*>([^<]+)<', html[i:i + 800])
    assert head and tile.group(1) in head.group(1), (
        f"плитка «{tile.group(1)}», а шапка экрана «{head.group(1) if head else '—'}»")
