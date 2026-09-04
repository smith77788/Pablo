"""Уведомление «собеседник удалил сообщение» должно вести прямо в переписку.

Разрыв: уведомление было тупиком. Оно сообщало, что и сколько удалили, но
чтобы это увидеть — особенно вложение, которое теперь можно открыть, — надо
было вручную зайти в приложение, найти раздел и нужный чат среди прочих.
Момент наивысшей ценности модуля («у вас на глазах стёрли сообщение») упирался
в ручной поиск.
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_H = (_ROOT / "bot" / "handlers" / "business_vault.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _handler_src(name: str) -> str:
    tree = ast.parse(_H)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(_H, node)
            assert seg is not None
            return seg
    raise AssertionError(f"{name} не найдена")


def test_deleted_notification_carries_a_button():
    src = _handler_src("on_deleted_business_messages")
    assert "reply_markup=_open_chat_kb(chat_id)" in src


def test_button_deep_links_to_that_exact_chat():
    src = _handler_src("_open_chat_kb")
    assert "#vault:{int(chat_id)}" in src, "ссылка обязана вести в КОНКРЕТНУЮ переписку"


def test_button_is_optional_when_mini_app_url_is_not_configured():
    """Без настроенного адреса мини-аппа уведомление должно уйти как раньше."""
    src = _handler_src("_open_chat_kb")
    assert "return None" in src
    assert "except Exception" in src


def test_chat_id_is_coerced_to_int():
    """В URL кнопки не должно попадать ничего, кроме числа."""
    assert "int(chat_id)" in _handler_src("_open_chat_kb")


def test_notification_mentions_saved_attachment():
    src = _handler_src("on_deleted_business_messages")
    assert "Вложение сохранено" in src
    assert 'm.get("media_label")' in src


# ── Разбор ссылки в мини-аппе ─────────────────────────────────────────────────

def test_ui_parses_kind_and_param():
    assert "_raw.indexOf(':')" in _UI
    assert "runPulseAction({kind: _screen, param: _param})" in _UI


def test_ui_opens_the_chat_for_vault_with_param():
    seg = _UI[_UI.index("function runPulseAction"):]
    seg = seg[:1200]
    assert "openVaultChat(Number(cid))" in seg


def test_ui_validates_the_param_is_numeric():
    """Параметр приходит из ссылки — в openVaultChat он попадать как есть не должен."""
    seg = _UI[_UI.index("function runPulseAction"):]
    seg = seg[:1200]
    assert "test(String(cid))" in seg


def test_plain_kind_without_param_still_works():
    """Старые ссылки вида #vault обязаны продолжать работать."""
    seg = _UI[_UI.index("function runPulseAction"):]
    seg = seg[:1200]
    assert "return openVault();" in seg
