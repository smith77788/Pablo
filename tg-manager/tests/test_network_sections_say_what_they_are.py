"""«Сети» различимы по подписи.

Аудит интерфейса (03.11.2026): в каталоге семь разделов про «сеть» —
Экосистемы, Сетки, Топология, Ноды, Сеть контента, Граф-хаб, Сеть ботов
(плюс Связки и Гео-сеть), — а подписи у них были почти одинаковые:
«Управление сетями ресурсов», «Сети ресурсов», «Карта связей ресурсов»,
«Узлы распределённой сети». Это РАЗНЫЕ системы с разными данными
(ecosystems, network_instances, сводка по флоту, bm_telegram_nodes,
content_meshes, пересечения аудиторий), и по подписи выбрать нужную было
нельзя. Подпись обязана говорить, что именно внутри.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

# «Связки (бот+каналы)» — теперь вкладка «Связки» модуля «Сеть ботов»;
# «Топология» и «Ноды» — вкладки модуля «Инфра», «Гео-сеть: создать» стала
# плиткой «Гео-сеть», «Сеть контента» — вкладка «Клонера контента»
# (tests/test_merged_modules.py). Своих подписей у вкладок нет.
NETWORK = ("Экосистемы", "Сетки", "Сеть ботов", "Инфра", "Граф-хаб",
           "Клонер контента", "Гео-сеть")
VAGUE = ("сетями ресурсов", "Сети ресурсов", "Карта связей ресурсов",
         "Узлы распределённой сети", "Инструменты графа связей",
         "Сеть распространения контента", "Связанная сеть ботов")


def _descs() -> dict[str, str]:
    html = miniapp_html()
    block = html[html.index("const MODULE_DESCS = {"):]
    block = block[:block.index("\n};")]
    return dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", block))


def test_every_network_section_has_its_own_description():
    d = _descs()
    missing = [n for n in NETWORK if n not in d]
    assert not missing, f"без подписи: {missing}"
    texts = [d[n] for n in NETWORK]
    assert len(set(texts)) == len(texts), "у двух «сетей» одинаковая подпись"


def test_no_vague_description_is_back():
    d = _descs()
    bad = {n: d[n] for n in NETWORK if any(v in d[n] for v in VAGUE)}
    assert not bad, f"подпись снова ни о чём: {bad}"
