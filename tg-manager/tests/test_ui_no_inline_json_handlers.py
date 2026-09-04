"""Обработчики в разметке не должны нести встроенный JSON.

Найдено попыткой сломать: кнопки правки собирались как
`onclick='editDm(${JSON.stringify(c)})'`. JSON.stringify экранирует двойные
кавычки, но НЕ апострофы — а атрибут обёрнут именно в апострофы. Название
кампании «О'Брайен» или текст шага со словом «don't» закрывали атрибут раньше
времени: кнопка ломалась, а разметка становилась управляемой содержимым.

Правильный приём (и он теперь применён): в атрибут кладём только id, объект
берём из кэша списка.
"""
from __future__ import annotations

import pathlib
import re

_UI = (pathlib.Path(__file__).resolve().parent.parent
       / "mini_app" / "index.html").read_text(encoding="utf-8")


# Храповик. Тот же приём остался ещё в четырёх местах — в Пульсе (runPulseAction,
# replayAction), в копировании текста и в Нодах. Это ДРУГИЕ модули, и чинить их
# в рамках работы над рассылкой было бы прыжком по проекту; фиксируем текущее
# число, чтобы новых не появлялось, а эти чинились вместе со своими модулями.
_KNOWN_SINGLE_QUOTED = 4


def test_no_new_inline_json_in_single_quoted_handlers():
    """Апостроф внутри JSON закрывает атрибут: JSON.stringify экранирует
    двойные кавычки, но не одинарные, а атрибут обёрнут именно в них."""
    bad = re.findall(r"on\w+\s*=\s*'[^']*JSON\.stringify", _UI)
    assert len(bad) <= _KNOWN_SINGLE_QUOTED, (
        f"появился новый обработчик со встроенным JSON в апострофах: {bad}"
    )


def test_outbound_module_handlers_are_clean():
    """В рассылке и воронках такого приёма быть не должно вовсе."""
    for needle in ("editDm(${JSON.stringify", "editFunnelStep(${JSON.stringify",
                   "launchDm(${JSON.stringify", "pauseDm(${JSON.stringify"):
        assert needle not in _UI, needle


def test_edit_handlers_take_only_an_id():
    assert 'onclick="editDm(${c.id})"' in _UI
    assert 'onclick="editFunnelStep(${s.id})"' in _UI


def test_edit_handlers_resolve_from_cache():
    assert "CUR_DM_CAMPAIGNS" in _UI and "CUR_FUNNEL_STEPS" in _UI
    assert "function editDm(id)" in _UI
    assert "function editFunnelStep(stepId)" in _UI


def test_missing_entity_is_reported_not_silent():
    """Кэш мог устареть — пользователь должен понять, что произошло."""
    assert "Кампания не найдена" in _UI
    assert "Шаг не найден" in _UI
