"""Контент → публикация: сохранённый шаблон вставляется в КОМПОЗЕР ПУБЛИКАЦИИ.

Баг-класс «мёртвая кнопка/тупик»: экран «Шаблоны» был, но useTpl жёстко писал только
в bcastText (сетевая рассылка). Пользователь, набравший пост в «Массовой публикации»
(mpText) или в DM-кампании (cmpText), не мог переиспользовать шаблон — тупик в
пути «контент → публикация».

Фикс: openTemplates(target) запоминает целевой композер (TPL_TARGET); useTpl пишет
в него и обновляет счётчик длины; у публикатора и кампании есть кнопка
«📝 Вставить шаблон».
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_open_templates_accepts_target():
    m = re.search(r"function openTemplates\((\w*)\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "openTemplates не найден"
    assert m.group(1), "openTemplates должен принимать target"
    assert "TPL_TARGET =" in m.group(2), "openTemplates должен запоминать целевой композер"


def test_use_tpl_writes_to_target_not_hardcoded():
    m = re.search(r"function useTpl\([^)]*\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "useTpl не найден"
    body = m.group(1)
    # пишет в TPL_TARGET, а не жёстко в bcastText
    assert "getElementById(TPL_TARGET)" in body
    # обновляет счётчик длины (через input-событие композера)
    assert "new Event('input'" in body
    # маппинг счётчиков покрывает публикатор и кампанию
    assert "mpText:'mpLen'" in body and "cmpText:'cmpLen'" in body


def test_publisher_and_campaign_have_insert_template_button():
    # публикатор
    assert "openTemplates('mpText')" in HTML, "у публикатора нет кнопки вставки шаблона"
    # DM-кампания
    assert "openTemplates('cmpText')" in HTML, "у кампании нет кнопки вставки шаблона"
    # быстрый пост
    assert "openTemplates('quickPostText')" in HTML, "у быстрого поста нет кнопки вставки шаблона"


def test_quick_post_reuses_mass_publish_for_spintax():
    """Быстрый пост НЕ должен заводить свой публикатор без spintax — он обязан
    переиспользовать _exec_mass_publish (в котором spintax на канал уже есть)."""
    from services import op_worker

    assert op_worker.handler_for("quick_post") is op_worker._exec_mass_publish, (
        "quick_post должен исполняться через _exec_mass_publish"
    )


def test_quick_post_ui_hints_spintax():
    i = HTML.index('id="quickPostText"')
    # подсказка про spintax рядом с полем
    seg = HTML[i - 200:i + 400]
    assert "Spintax" in seg or "spintax" in seg.lower()
