"""«Нет аккаунтов» и «Нет ботов» — экран с кнопкой добавления, не всплывашка.

Всплывашка «Нет аккаунтов.» закрывается одним нажатием, и человек остаётся
на том же экране, не зная, где аккаунт добавляется. 23.11.2026 так отвечали
15 кнопок: запуск жалоб, бустов, смены профилей, клонера, инвайтера, фабрик
каналов и групп, массовых операций, передача подарков, выгрузка здоровья,
проверка ботов сети. Теперь они показывают экран `show_no_accounts` /
`show_no_bots`, где первая кнопка — «➕ Добавить аккаунт» / «➕ Добавить бота».

Сторож не пускает обратно ответ на нажатие, который сообщает только
«нет аккаунтов / ботов» и не говорит, как их добавить.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"

_NO_FLEET = re.compile(
    r"^\W*нет\s+(активных\s+|доступных\s+|подключённых\s+)?(аккаунтов|ботов)\b",
    re.IGNORECASE,
)
_CB_NAMES = {"callback", "cb", "call", "query", "c"}


def fleet_popups(source: str) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "answer" and node.args):
            continue
        if ast.unparse(node.func.value) not in _CB_NAMES:
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        text = arg.value
        if _NO_FLEET.search(text) and "добав" not in text.lower():
            out.append((node.lineno, text))
    return out


def test_detector_sees_known_popup() -> None:
    src = (
        "async def h(callback):\n"
        "    await callback.answer('Нет аккаунтов.', show_alert=True)\n"
        "    await cb.answer('⚠️ Нет доступных аккаунтов', show_alert=True)\n"
        "    await callback.answer('Нет ботов.', show_alert=True)\n"
        "    await callback.answer('Нет активных аккаунтов. Добавьте аккаунт сначала.')\n"
        "    await callback.answer('Нет других ботов для копирования.')\n"
    )
    assert [line for line, _ in fleet_popups(src)] == [2, 3, 4]


def test_no_fleet_popup_without_add_button() -> None:
    found = []
    for path in sorted(HANDLERS.rglob("*.py")):
        for line, text in fleet_popups(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{line}: {text!r}")
    assert not found, (
        "Всплывашка «нет аккаунтов/ботов» без пути к добавлению — "
        "используйте show_no_accounts(callback) / show_no_bots(callback):\n"
        + "\n".join(found)
    )


def _helper(name: str) -> str:
    src = (ROOT / "bot" / "utils" / "op_helpers.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} не найден")


def test_helpers_lead_to_adding() -> None:
    assert "no_accounts_kb(" in _helper("show_no_accounts")
    assert "no_bots_kb(" in _helper("show_no_bots")
