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
import re

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


def _run_pulse_action() -> str:
    """Тело runPulseAction целиком, по балансу фигурных скобок.

    Раньше здесь стоял срез `[:1200]` от первого вхождения «function
    runPulseAction» — а первой в файле идёт runPulseActionById, так что окно
    должно было покрыть обе функции. Дописанный в код комментарий сдвинул
    ветку vault за границу окна, и три проверки покраснели на ЗДОРОВОМ коде:
    ровно то, что запрещает храповик test_no_silently_disabled_guards. Границы
    берём по структуре, а не по числу символов.
    """
    m = re.search(r"^function runPulseAction\(", _UI, re.M)
    assert m, "runPulseAction не найдена"
    i = _UI.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(_UI)):
        if _UI[j] == "{":
            depth += 1
        elif _UI[j] == "}":
            depth -= 1
            if depth == 0:
                return _UI[i:j + 1]
    raise AssertionError("не закрылось тело runPulseAction")


def test_ui_opens_the_chat_for_vault_with_param():
    seg = _run_pulse_action()
    assert "openVaultChat(Number(cid))" in seg


def test_ui_validates_the_param_is_numeric():
    """Параметр приходит из ссылки — в openVaultChat он попадать как есть не должен."""
    seg = _run_pulse_action()
    assert "test(String(cid))" in seg


def test_plain_kind_without_param_still_works():
    """Старые ссылки вида #vault обязаны продолжать работать."""
    seg = _run_pulse_action()
    assert "return openVault();" in seg
