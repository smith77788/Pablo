"""Паритет: account_cleaner в боте умеет read_all_dialogs / delete_private_dialogs.

Эти операции раньше запускались только из mini-app (account_action act
read_all/delete_pm). Executors в op_worker уже есть и покрыты отдельно
(test_read_all_dialogs.py) — здесь проверяем именно бот-сторону: меню-кнопки,
хендлеры и постановку через operation_bus с корректными op_type + подтверждение
перед необратимым удалением.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_cleaner_menu_has_read_all_and_del_pm_buttons():
    h = _read("bot/handlers/account_cleaner.py")
    assert 'CleanerCb(action="read_all")' in h
    assert 'CleanerCb(action="del_pm")' in h


def test_cleaner_has_all_parity_handlers():
    h = _read("bot/handlers/account_cleaner.py")
    for act in ("read_all", "do_read_all", "del_pm", "confirm_del_pm", "do_del_pm"):
        assert f'CleanerCb.filter(F.action == "{act}")' in h, f"нет хендлера {act}"


def test_cleaner_enqueues_correct_op_types_via_bus():
    h = _read("bot/handlers/account_cleaner.py")
    # read_all_dialogs через operation_bus (прямой INSERT в новых хендлерах запрещён)
    assert 'operation_bus.submit(' in h
    assert '"read_all_dialogs", {"account_id": acc_id}' in h
    assert '"delete_private_dialogs", {"account_id": acc_id}' in h


def test_delete_pm_requires_confirmation_step():
    """Необратимое удаление ЛС не запускается по одному тапу — сначала confirm."""
    h = _read("bot/handlers/account_cleaner.py")
    # del_pm ведёт на confirm_del_pm (пикер), а не сразу на do_del_pm
    assert '"confirm_del_pm"' in h
    # do_del_pm вызывается только из кнопки внутри confirm-экрана
    assert 'CleanerCb(action="do_del_pm", account_id=acc_id)' in h


def test_op_registry_has_both_op_types():
    reg = _read("services/operation_bus.py")
    assert '"read_all_dialogs":' in reg
    assert '"delete_private_dialogs":' in reg
