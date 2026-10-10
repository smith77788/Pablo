"""Пресеты: подстановка плейсхолдеров перед применением к боту (класс #3/#4).

Баг из прод-скрина: пользователь применил шаблон бота из меню бота
(bots.py::cb_pset_apply) — этот путь писал сырой `preset["template"]` БЕЗ подстановки
плейсхолдеров, поэтому конечные пользователи бота получали дословно
`{{COMPANY}}`/`{{HOURS}}`/`{{OPERATOR_LINE}}` (и настроить данные было негде).

Фиксируем: (1) канонические default_subs+render_template не оставляют сырых токенов
ни в одном текстовом поле шаблона; (2) OPERATOR_LINE выводится, а не течёт сырым;
(3) оба хендлера (per-bot меню и библиотека) реально прогоняют шаблон через подстановку.
"""
from __future__ import annotations

import pathlib
import re

from services.preset_templates import (
    default_subs,
    get_presets,
    get_preset_by_key,
    render_template,
)

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)


def test_default_subs_leaves_no_raw_placeholders_in_any_bot_preset():
    """Применение с дефолтами не оставляет ни одного сырого {{TOKEN}}."""
    presets = get_presets("bot")
    assert presets, "должны быть пресеты бота"
    for preset in presets:
        rendered = render_template(preset["template"], default_subs(preset))
        leaks = [s for s in _all_strings(rendered) if _PLACEHOLDER.search(s)]
        assert not leaks, (
            f"пресет {preset['id']}: сырые плейсхолдеры дошли до текста: {leaks}"
        )


def test_operator_line_derived_not_leaked():
    """OPERATOR_LINE — производное поле: без оператора пусто, не сырой токен."""
    preset = get_preset_by_key("bot__support_bot")
    assert preset, "support_bot пресет должен существовать"
    subs = default_subs(preset)
    assert "OPERATOR_LINE" in subs
    # дефолт support_bot: оператор пуст → строка оператора пустая
    assert subs["OPERATOR_LINE"] == ""
    rendered = render_template(preset["template"], subs)
    for s in _all_strings(rendered):
        assert "{{OPERATOR_LINE}}" not in s
        assert "{{OPERATOR}}" not in s


def test_operator_line_filled_when_operator_set():
    """Если оператор задан — подставляется ' пишите @op', а не пусто/сырой токен."""
    preset = get_preset_by_key("bot__support_bot")
    subs = default_subs(preset)
    subs["OPERATOR"] = "@support_manager"
    # эмулируем ветку задания оператора (как в FSM/дефолтах)
    op = subs["OPERATOR"].lstrip("@")
    subs["OPERATOR_LINE"] = f" пишите @{op}"
    rendered = render_template(preset["template"], subs)
    joined = "\n".join(_all_strings(rendered))
    assert "@support_manager" in joined


def test_render_template_is_pure_deepcopy():
    """render_template не мутирует исходный шаблон (важно: PRESETS — глобал)."""
    preset = get_preset_by_key("bot__support_bot")
    before = preset["template"]["welcome_message"]
    render_template(preset["template"], {"COMPANY": "X", "HOURS": "Y"})
    assert preset["template"]["welcome_message"] == before
    assert "{{COMPANY}}" in before  # оригинал по-прежнему с плейсхолдером


def test_both_handlers_substitute_before_apply():
    """Оба пути применения обязаны прогонять шаблон через подстановку.

    Регресс против возврата сырого `preset["template"]` в bots.py.
    """
    root = pathlib.Path(__file__).resolve().parents[1]

    import ast
    bots = (root / "bot" / "handlers" / "bots.py").read_text(encoding="utf-8")
    # Границы функции по AST: срез фиксированной длины сдвигается вместе с кодом,
    # и отрицательная проверка ниже («сырой template не пишем») молча выключается.
    body = None
    for node in ast.walk(ast.parse(bots)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "cb_pset_apply":
            body = "\n".join(bots.split("\n")[node.lineno - 1:node.end_lineno])
    assert body, "cb_pset_apply не найден"
    assert "render_template" in body and "default_subs" in body, (
        "cb_pset_apply должен подставлять плейсхолдеры (render_template/default_subs)"
    )
    # и не писать сырой template напрямую
    assert 'tpl = preset["template"]\n' not in body, "cb_pset_apply пишет сырой template"

    at = (root / "bot" / "handlers" / "asset_templates.py").read_text(encoding="utf-8")
    # библиотечный дефолтный путь тоже через default_subs (OPERATOR_LINE не течёт)
    assert "default_subs(preset)" in at
