"""Регресс: плейсхолдеры шаблона поста ({{CITY}}, {{COUNTRY}}, ...) уходили в
реальные каналы буквально, не подставляясь.

Первопричина. `bot/handlers/asset_templates.py` явно рекламирует
{{USERNAME}}/{{FIRST_NAME}}/{{DATE}}/{{BOT_NAME}}/{{CHANNEL}}/{{CITY}}/
{{COUNTRY}} как поддерживаемые плейсхолдеры постов (`asset_type='post'`).
Но оба реальных пути публикации в каналы — `quick_post.py::cb_qp_use_template`
(и ручной ввод текста `msg_qp_text`) и `mass_publish.py`'s tpl_prefill/ручной
ввод — брали текст как есть и клали в `post_text`/`op_params["text"]` без
единого вызова `replace_placeholders`/`list_placeholders`. Пользователь,
следующий собственной подсказке интерфейса, получал `{{CITY}}` буквально в
опубликованном посте.

Фикс: {{DATE}}/{{DATE_SHORT}} — не зависят от канала, подставляются сразу
(`auto_fillable_placeholders`). CITY/COUNTRY/USERNAME/CHANNEL/BOT_NAME
подставить некем (нет получателя-пользователя, список каналов ещё не выбран),
поэтому вместо тихой отправки мусора — явное предупреждение на экране
подтверждения (`unresolved_placeholders_warning`).

Мини-апп в этот баг НЕ попадает: там `useTpl()` вставляет текст в
РЕДАКТИРУЕМЫЙ textarea (пользователь видит и может исправить {{CITY}} сам,
см. test_template_into_publisher.py) — в отличие от бота, где текст сразу
уходит в состояние и пользователь его больше не видит до экрана подтверждения.
"""
from __future__ import annotations

import ast
import pathlib

from bot.utils.template_validator import (
    auto_fillable_placeholders,
    list_placeholders,
    replace_placeholders,
    unresolved_placeholders_warning,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _func_source(relpath: str, funcname: str) -> str:
    path = ROOT / relpath
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            segment = ast.get_source_segment(src, node)
            assert segment is not None
            return segment
    raise AssertionError(f"функция {funcname!r} не найдена в {relpath}")


# ── Юнит: сами хелперы ───────────────────────────────────────────────────────

def test_auto_fillable_placeholders_covers_date_only():
    vals = auto_fillable_placeholders()
    assert set(vals) == {"DATE", "DATE_SHORT"}
    assert vals["DATE"]  # непустое, реально отформатированная дата


def test_replace_placeholders_with_auto_fillable_resolves_date():
    text = "Сегодня {{DATE}}, канал {{CHANNEL}}"
    out = replace_placeholders(text, auto_fillable_placeholders())
    assert "{{DATE}}" not in out
    assert "{{CHANNEL}}" in out, "CHANNEL нельзя подставить автоматически — оставляем как есть"


def test_unresolved_placeholders_warning_empty_when_nothing_left():
    assert unresolved_placeholders_warning("обычный текст без плейсхолдеров") == ""


def test_unresolved_placeholders_warning_fires_even_for_auto_fillable_keys():
    """Сама функция ничего не подставляет — это забота вызывающего кода
    (auto_fillable_placeholders + replace_placeholders ДО вызова этой функции).
    Если вызывающий забыл это сделать, предупреждение обязано появиться и для
    {{DATE}} — иначе баг «не подставили» останется незамеченным."""
    assert unresolved_placeholders_warning("текст {{DATE}}") != ""


def test_unresolved_placeholders_warning_lists_the_keys():
    w = unresolved_placeholders_warning("Привет из {{CITY}}, {{COUNTRY}}!")
    assert "{{CITY}}" in w and "{{COUNTRY}}" in w
    assert "⚠️" in w


# ── quick_post.py: оба пути ввода текста подставляют DATE, экран подтверждения предупреждает ──

def test_quick_post_template_path_auto_fills_date():
    src = _func_source("bot/handlers/quick_post.py", "cb_qp_use_template")
    assert "replace_placeholders(text, auto_fillable_placeholders())" in src


def test_quick_post_manual_text_path_auto_fills_date():
    src = _func_source("bot/handlers/quick_post.py", "msg_qp_text")
    assert "replace_placeholders(text, auto_fillable_placeholders())" in src


def test_quick_post_confirm_screen_warns_about_leftover_placeholders():
    src = _func_source("bot/handlers/quick_post.py", "cb_qp_timing")
    assert "unresolved_placeholders_warning(post_text)" in src
    assert "warn_line" in src


# ── mass_publish.py: оба tpl_prefill-пути + ручной ввод + экран предпросмотра ──

def test_mass_publish_start_prefill_auto_fills_date():
    src = _func_source("bot/handlers/mass_publish.py", "cb_mpub_start")
    assert "replace_placeholders(prefill_text, auto_fillable_placeholders())" in src


def test_mass_publish_pick_account_prefill_auto_fills_date():
    src = _func_source("bot/handlers/mass_publish.py", "cb_mpub_pick_account")
    assert "replace_placeholders(prefill_text, auto_fillable_placeholders())" in src


def test_mass_publish_manual_text_auto_fills_date():
    src = _func_source("bot/handlers/mass_publish.py", "fsm_mpub_text")
    assert "replace_placeholders(text, auto_fillable_placeholders())" in src


def test_mass_publish_preview_warns_about_leftover_placeholders():
    src = _func_source("bot/handlers/mass_publish.py", "_show_preview")
    assert "unresolved_placeholders_warning(post_text)" in src
    assert "warn_line" in src
