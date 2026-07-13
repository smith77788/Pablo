"""Паритет: проверка ресурсов на запрещёнку из бота (compliance_scan).

Read-only проверка каналов/групп на запрещённую тематику запускалась только из
mini-app (compliance_scan_submit). Бот получил /scan_resources поверх того же op
'compliance_scan' через operation_bus. Исполнитель уже существует.
"""
from __future__ import annotations

import os

import tests.conftest  # noqa: F401 — стабы telethon/aiogram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_handler_imports_and_router():
    import bot.handlers.compliance_scan as cs
    assert cs.router is not None
    for fn in ("cmd_scan", "cb_scan_open", "cb_scan_cancel", "msg_scan_resources"):
        assert hasattr(cs, fn), f"нет хендлера {fn}"


def test_submits_compliance_scan_op():
    h = _read("bot/handlers/compliance_scan.py")
    assert "operation_bus.submit(" in h
    assert '"compliance_scan"' in h
    for key in ('"resources"', '"per_resource_limit"', '"acc_count"'):
        assert key in h, f"нет параметра {key}"


def test_op_registered_and_executor_exists():
    assert '"compliance_scan":' in _read("services/operation_bus.py")
    assert "async def _exec_compliance_scan(" in _read("services/op_worker.py")


def test_router_registered_in_main():
    m = _read("main.py")
    assert "compliance_scan as compliance_scan_handler" in m
    assert "dp.include_router(compliance_scan_handler.router)" in m


def test_command_present():
    assert 'Command("scan_resources")' in _read("bot/handlers/compliance_scan.py")


def test_caps_resources_at_100():
    h = _read("bot/handlers/compliance_scan.py")
    assert "[:100]" in h  # потолок против гигантских прогонов, как в mini-app
