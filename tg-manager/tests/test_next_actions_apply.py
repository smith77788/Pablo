"""Регресс-тесты ядра «применить в один клик» (Copilot apply).

Проверяем _retry_failed_ops_core: mass_publish пропускается (иначе дубли постов),
остальные упавшие ре-сабмятся ЧЕРЕЗ operation_bus (не прямым INSERT), JSON-params
корректно парсятся.
"""

import services.operation_bus as obus
from services.mini_app_api import _retry_failed_ops_core


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, q, *a):
        return self._rows


async def test_retry_skips_mass_publish_and_submits_rest(monkeypatch):
    calls = []

    async def fake_submit(pool, owner_id, op_type, params, **kw):
        calls.append({"op_type": op_type, "params": params,
                      "total_items": kw.get("total_items"), "label": kw.get("label")})
        return 999

    monkeypatch.setattr(obus, "submit", fake_submit)

    rows = [
        {"op_type": "mass_invite", "params": {"x": 1}, "label": "Инвайт", "total_items": 10},
        {"op_type": "mass_publish", "params": {"y": 2}, "label": "Публикация", "total_items": 5},
        {"op_type": "run_broadcast", "params": '{"z": 3}', "label": "Рассылка", "total_items": 7},
    ]
    res = await _retry_failed_ops_core(_Pool(rows), uid=42)

    assert res["retried"] == 2          # mass_invite + run_broadcast
    assert res["skipped"] == 1          # mass_publish
    op_types = [c["op_type"] for c in calls]
    assert "mass_publish" not in op_types
    assert set(op_types) == {"mass_invite", "run_broadcast"}
    # JSON-строка params распарсилась в dict
    br = next(c for c in calls if c["op_type"] == "run_broadcast")
    assert br["params"] == {"z": 3}
    assert br["total_items"] == 7


async def test_retry_handles_broken_params(monkeypatch):
    async def fake_submit(pool, owner_id, op_type, params, **kw):
        assert isinstance(params, dict)   # никогда не отдаём не-dict в шину
        return 1

    monkeypatch.setattr(obus, "submit", fake_submit)
    rows = [
        {"op_type": "bulk_join", "params": "not-json", "label": None, "total_items": None},
        {"op_type": "bulk_leave", "params": None, "label": None, "total_items": 0},
    ]
    res = await _retry_failed_ops_core(_Pool(rows), uid=1)
    assert res["retried"] == 2


async def test_retry_empty(monkeypatch):
    async def fake_submit(*a, **k):
        raise AssertionError("submit не должен вызываться при пустом списке")

    monkeypatch.setattr(obus, "submit", fake_submit)
    res = await _retry_failed_ops_core(_Pool([]), uid=1)
    assert res == {"ok": True, "retried": 0, "skipped": 0}
