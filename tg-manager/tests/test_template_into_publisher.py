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
    # Третьей кнопки — у «Быстрого поста» — больше нет: экран слит с единой
    # публикацией, и вставка шаблона у неё та же самая, openTemplates('mpText').


def test_quick_post_reuses_mass_publish_for_spintax():
    """Быстрый пост НЕ должен заводить свой публикатор без spintax — он обязан
    переиспользовать _exec_mass_publish (в котором spintax на канал уже есть)."""
    from services import op_worker

    assert op_worker.handler_for("quick_post") is op_worker._exec_mass_publish, (
        "quick_post должен исполняться через _exec_mass_publish"
    )


def test_publisher_ui_hints_spintax():
    """Подсказка про спинтакс стоит у поля, из которого пост уходит.

    Проверка была привязана к полю удалённого «Быстрого поста»
    (`quickPostText`): после слияния экранов она либо падала, либо охраняла
    подсказку, которой владелец уже не видит. Публикует единый экран —
    значит и подсказка проверяется у него.
    """
    i = HTML.index('id="mpText"')
    # Слово пишется кириллицей: владелец не читает по-английски, и латинские
    # названия из интерфейса убраны.
    seg = HTML[i - 200:i + 700]
    assert "спинтакс" in seg.lower() or "spintax" in seg.lower(), (
        "у публикатора пропала подсказка про спинтакс"
    )
    assert "{Привет|Здравствуйте}" in seg, (
        "подсказка без примера синтаксиса не объясняет, что писать"
    )
