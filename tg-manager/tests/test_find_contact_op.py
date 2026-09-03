"""Регресс: операция find_contact зарегистрирована, задиспатчена, эндпоинт поднят,
и честный итог собирается верно (совпадение / нет совпадения+продолжение).
"""
from __future__ import annotations

import re
from pathlib import Path
from services import op_worker

ROOT = Path(__file__).resolve().parents[1]


def test_find_contact_registered_and_dispatched():
    reg = (ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    api = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"find_contact":' in reg, "find_contact не в OP_REGISTRY"
    assert op_worker.handler_for("find_contact") is not None, "нет ветки диспетчера find_contact"
    assert "async def _exec_find_contact(" in worker, "нет исполнителя _exec_find_contact"
    assert '"/api/miniapp/find_contact"' in api, "маршрут find_contact не зарегистрирован"


def test_summary_match_found():
    from services.op_worker import _find_contact_summary
    matches = [{"username": "Smile042", "user_id": 5, "name": "Oracle", "premium": False}]
    out = _find_contact_summary("Oracle", "Smile", matches, {"smile042": matches[0]},
                                checked=43, total_planned=1000)
    assert out["status"] == "done" and out["found"] == 1
    assert "Oracle" in out["summary"] and "t.me/Smile042" in out["summary"]


def test_summary_no_match_offers_continuation():
    from services.op_worker import _find_contact_summary
    found = {"smile007": {"username": "Smile007", "name": "Кто-то", "user_id": 1, "premium": False}}
    out = _find_contact_summary("Oracle", "Smile", [], found,
                                checked=2000, total_planned=8000, next_offset=2000)
    assert out["found"] == 0
    assert "offset=2000" in out["summary"]           # честное продолжение остатка
    assert "не найдено" in out["summary"].lower()
